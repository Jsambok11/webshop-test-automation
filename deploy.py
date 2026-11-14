#!/usr/bin/env python3
"""One-command deployment of the web shop to the local Kubernetes cluster.

Steps:
  1. check tools and select the docker-desktop context
  2. create the Kubernetes Secret with random passwords (only the first time)
  3. build the Docker images, tagged with a hash of the source code
  4. apply all manifests in k8s/ and re-run the database init job
  5. wait until every deployment is rolled out and ready

Requires Docker Desktop with Kubernetes enabled. Run from the project root.
Uses only the Python standard library, so nothing has to be installed first.
"""
import hashlib
import os
import re
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

SECRET_NAME = "app-secrets"
APP_DEPLOYMENTS = ["user-service", "product-service", "order-service", "frontend-service"]
ROOT = Path(__file__).parent
IMAGE_LINE = re.compile(r"(image: examination-[a-z-]+):\S+")


def run(cmd, check=True, capture=False):
    print(f"$ {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=capture, text=True)
    if check and result.returncode != 0:
        if capture:
            print(result.stdout + result.stderr)
        print(f"[ERROR] Command failed: {' '.join(cmd)}")
        sys.exit(result.returncode or 1)
    return result


def sql_password():
    # Random, and always meets SQL Server's password policy
    # (upper case, lower case and digits, longer than 8 characters).
    return secrets.token_hex(16) + "Aa1"


def ensure_secret():
    """Create the Secret once. It is NOT recreated on later deploys, because the
    database on the persistent volume keeps using the passwords it was created with."""
    exists = run(["kubectl", "get", "secret", SECRET_NAME], check=False, capture=True).returncode == 0
    if exists:
        print(f"[OK] Secret {SECRET_NAME} already exists, keeping it.")
        return
    values = {
        "sa-password": sql_password(),
        "user-db-password": sql_password(),
        "product-db-password": sql_password(),
        "order-db-password": sql_password(),
        "internal-api-token": secrets.token_urlsafe(32),
        "flask-secret-key": secrets.token_hex(32),
    }
    cmd = ["kubectl", "create", "secret", "generic", SECRET_NAME]
    cmd += [f"--from-literal={k}={v}" for k, v in values.items()]
    # Do not echo the command: it contains the secret values.
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stderr)
        sys.exit("[ERROR] Could not create the Secret")
    subprocess.run(["kubectl", "label", "secret", SECRET_NAME, "app.kubernetes.io/part-of=webshop"],
                   capture_output=True)
    print(f"[OK] Created Secret {SECRET_NAME} with random values.")


def source_tag():
    """Image tag derived from the service source code, e.g. "1.2.0-3f9a2c1d7e0b".

    Every code change gives a new tag, so Kubernetes always runs the code that
    was just built. (With a fixed tag the node keeps using its cached image.)
    Unchanged code gives the same tag, so a redeploy changes nothing.
    """
    digest = hashlib.sha256()
    for path in sorted((ROOT / "services").rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            digest.update(path.relative_to(ROOT).as_posix().encode())
            digest.update(path.read_bytes())
    return f"1.2.0-{digest.hexdigest()[:12]}"


def apply_manifests(tag):
    """Apply k8s/*.yaml with the app images pointed at this build's tag."""
    docs = [IMAGE_LINE.sub(lambda m: f"{m.group(1)}:{tag}", f.read_text(encoding="utf-8"))
            for f in sorted((ROOT / "k8s").glob("*.yaml"))]
    print(f"$ kubectl apply -f k8s/  (images tagged {tag})")
    result = subprocess.run(["kubectl", "apply", "-f", "-"], input="\n---\n".join(docs),
                            text=True, encoding="utf-8")
    if result.returncode != 0:
        sys.exit("[ERROR] kubectl apply failed")


def compose_command():
    if run(["docker", "compose", "version"], check=False, capture=True).returncode == 0:
        return ["docker", "compose"]
    if shutil.which("docker-compose"):
        return ["docker-compose"]
    sys.exit("[ERROR] Neither 'docker compose' nor 'docker-compose' is available.")


def main():
    print("=" * 50)
    print("Web shop - automated deployment")
    print("=" * 50)

    for tool in ("kubectl", "docker"):
        if not shutil.which(tool):
            sys.exit(f"[ERROR] {tool} not found. Is Docker Desktop installed with Kubernetes enabled?")
    run(["kubectl", "config", "use-context", "docker-desktop"], check=False)

    print("\n[1/4] Ensuring secrets...")
    ensure_secret()

    print("\n[2/4] Building Docker images...")
    tag = source_tag()
    os.environ["IMAGE_TAG"] = tag  # read by docker-compose.yaml
    run(compose_command() + ["build"])

    print("\n[3/4] Applying Kubernetes manifests...")
    # A finished Job cannot be started again, so remove the old one first.
    run(["kubectl", "delete", "job", "db-init-job", "--ignore-not-found"])
    apply_manifests(tag)

    print("\nWaiting for the database to be initialized...")
    if run(["kubectl", "wait", "--for=condition=complete", "job/db-init-job", "--timeout=300s"],
           check=False).returncode != 0:
        run(["kubectl", "logs", "job/db-init-job", "--tail=50"], check=False)
        sys.exit("[ERROR] Database initialization did not complete.")

    print("\n[4/4] Waiting for the services to roll out...")
    for name in ["mssql-deployment", *APP_DEPLOYMENTS]:
        run(["kubectl", "rollout", "status", f"deployment/{name}", "--timeout=180s"])

    print("\n[OK] Deployment completed.")
    print("Open the shop:   kubectl port-forward service/frontend-service 8080:80")
    print("                 then browse to http://localhost:8080")


if __name__ == "__main__":
    main()
