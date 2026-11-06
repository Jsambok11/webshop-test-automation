#!/usr/bin/env python3
"""Remove the web shop's Kubernetes resources.

  python cleanup.py              delete everything in k8s/, including the database volume
  python cleanup.py --keep-data  delete everything except the database volume
  python cleanup.py --purge      also delete the Secret (new passwords on next deploy)

The Secret is kept by default: a database volume created with one set of
passwords cannot be used with another.
"""
import argparse
import subprocess
from pathlib import Path

K8S_DIR = Path(__file__).parent / "k8s"
PVC_FILE = "mssql-pvc.yaml"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--keep-data", action="store_true", help="keep the database volume")
    group.add_argument("--purge", action="store_true", help="also delete the Secret")
    args = parser.parse_args()

    if args.keep_data:
        files = sorted(p for p in K8S_DIR.glob("*.yaml") if p.name != PVC_FILE)
        print("Deleting Kubernetes resources (keeping the database volume)...")
        for f in files:
            subprocess.run(["kubectl", "delete", "-f", str(f), "--ignore-not-found"], check=False)
    else:
        print("Deleting all Kubernetes resources from k8s/ (including the database volume)...")
        subprocess.run(["kubectl", "delete", "-f", str(K8S_DIR), "--ignore-not-found"], check=False)

    if args.purge:
        subprocess.run(["kubectl", "delete", "secret", "app-secrets", "--ignore-not-found"], check=False)

    print("Cleanup complete.")


if __name__ == "__main__":
    main()
