"""Helpers for integration tests against the deployed system (python deploy.py).

- kubectl / Secret access
- port-forwarding the services to localhost
- running SQL inside the SQL Server pod
- building a logged-in frontend session without using the login form
- small polling helper for things that take time (pods restarting, logs)
"""
import base64
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SERVICES = {  # short name -> (Kubernetes Service, local port)
    "user": ("user-service", 15002),
    "product": ("product-service", 15001),
    "order": ("order-service", 15003),
    "frontend": ("frontend-service", 18080),
}
BACKENDS = ["user-service", "product-service", "order-service"]


# ------------------------------------------------------------------ kubectl
def kubectl(*args, check=True, timeout=120):
    result = subprocess.run(["kubectl", *args], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=timeout)
    if check and result.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)} failed:\n{result.stdout}{result.stderr}")
    return result


def read_secret(key):
    """One value from the Secret app-secrets that deploy.py created."""
    out = kubectl("get", "secret", "app-secrets", "-o", f"jsonpath={{.data.{key}}}").stdout
    return base64.b64decode(out).decode()


def pods(app):
    """Pods of one app as dicts: name, ready, restarts."""
    data = json.loads(kubectl("get", "pods", "-l", f"app={app}", "-o", "json").stdout)
    result = []
    for pod in data["items"]:
        if pod["metadata"].get("deletionTimestamp"):
            continue
        statuses = pod["status"].get("containerStatuses", [])
        result.append({
            "name": pod["metadata"]["name"],
            "ready": bool(statuses) and all(s["ready"] for s in statuses),
            "restarts": sum(s["restartCount"] for s in statuses),
        })
    return result


def wait_until(condition, timeout, message, interval=2):
    """Poll `condition()` until it returns something truthy, or fail after `timeout` s."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(f"Timed out after {timeout}s: {message}")


# ------------------------------------------------------------------ port-forward
class PortForwards:
    """`kubectl port-forward` for every service.

    A port-forward is bound to one pod and is fragile: it ends when the pod is
    replaced (redeploy), and kubectl sometimes drops a connection or exits,
    e.g. under many parallel requests. Two safety nets:
      ensure()   before every test: restart forwards whose process has exited
      recover()  after a connection error: restart forwards that do not answer
    kubectl's output goes to reports/port-forward-<service>.log."""

    LOG_DIR = PROJECT_ROOT / "reports"
    current = None   # the running instance, used by ResilientSession

    def __init__(self):
        self.procs = {}
        self.urls = {name: f"http://localhost:{port}" for name, (_svc, port) in SERVICES.items()}
        PortForwards.current = self
        # Start each test run with empty logs; restarts during the run append.
        self.LOG_DIR.mkdir(exist_ok=True)
        for service, _port in SERVICES.values():
            (self.LOG_DIR / f"port-forward-{service}.log").write_text("", encoding="utf-8")

    def _start_one(self, name):
        service, port = SERVICES[name]
        self.LOG_DIR.mkdir(exist_ok=True)
        log = open(self.LOG_DIR / f"port-forward-{service}.log", "a", encoding="utf-8")
        self.procs[name] = subprocess.Popen(
            ["kubectl", "port-forward", f"service/{service}", f"{port}:80"],
            stdout=log, stderr=subprocess.STDOUT)
        log.close()   # the child process keeps its own handle
        wait_until(lambda: _is_live(self.urls[name]), 30,
                   f"{self.urls[name]} is not reachable through port-forward", 0.5)

    def start(self):
        for name in SERVICES:
            self._start_one(name)

    def ensure(self):
        """Restart every port-forward whose kubectl process has exited."""
        for name, proc in list(self.procs.items()):
            if proc.poll() is not None:
                self._start_one(name)

    def recover(self):
        """Restart every port-forward that has exited or no longer answers."""
        for name, proc in list(self.procs.items()):
            if proc.poll() is not None or not _is_live(self.urls[name]):
                proc.kill()
                proc.wait(timeout=10)
                self._start_one(name)

    def stop(self):
        for proc in self.procs.values():
            proc.terminate()
        for proc in self.procs.values():
            proc.wait(timeout=10)
        self.procs.clear()

    def restart(self):
        self.stop()
        self.start()


def _is_live(url):
    try:
        return requests.get(f"{url}/live", timeout=2).status_code == 200
    except requests.RequestException:
        return False


class ResilientSession(requests.Session):
    """requests.Session that survives a dropped port-forward.

    On a connection error it restarts the broken port-forward and sends the
    request once more, but only when that cannot change the result of the
    test: the request never reached the server (connection refused), or it is
    a GET/DELETE, which can safely be repeated. A POST that may already have
    been processed is never sent twice."""

    REPEATABLE = {"GET", "HEAD", "DELETE"}

    def request(self, method, url, *args, **kwargs):
        kwargs.setdefault("timeout", 30)
        try:
            return super().request(method, url, *args, **kwargs)
        except requests.ConnectionError as exc:
            never_sent = "NewConnectionError" in repr(exc) or "Connection refused" in repr(exc)
            if PortForwards.current is None or not (never_sent or method.upper() in self.REPEATABLE):
                raise
            PortForwards.current.recover()
            return super().request(method, url, *args, **kwargs)


# ------------------------------------------------------------------ SQL
class Sql:
    """Runs sqlcmd inside the SQL Server pod. Returns (exit code, output)."""

    def __init__(self):
        self.sa_password = read_secret("sa-password")

    def run(self, query, user="sa", password=None, database="master"):
        pod = kubectl("get", "pod", "-l", "app=mssql", "-o", "jsonpath={.items[0].metadata.name}").stdout
        result = kubectl(
            "exec", pod, "--", "/opt/mssql-tools18/bin/sqlcmd", "-S", "localhost", "-C",
            "-U", user, "-P", password or self.sa_password, "-d", database,
            "-b",          # exit code 1 when the statement fails
            "-I",          # QUOTED_IDENTIFIER ON, as the services' ODBC driver uses
                           # (OrdersDB has a filtered index that requires it)
            "-h", "-1",    # no column headers
            "-W",          # trim trailing spaces
            "-Q", f"SET NOCOUNT ON; {query}",
            check=False)
        return result.returncode, (result.stdout + result.stderr).strip()


# ------------------------------------------------------------------ frontend
def logged_in_cookie(data):
    """A valid frontend session cookie for `data` (e.g. user_id, cart).

    Signed with the frontend's own SECRET_KEY from the Secret, exactly as the
    frontend would sign it. This lets tests start "already logged in" without
    using the login form, which is rate limited (5 per minute) and tested
    separately (IT-034)."""
    from flask import Flask
    from flask.sessions import SecureCookieSessionInterface

    app = Flask("integration-tests")
    app.secret_key = read_secret("flask-secret-key")
    return SecureCookieSessionInterface().get_signing_serializer(app).dumps(data)


def form_value(html, name):
    """Value of a hidden form field such as csrf_token or checkout_token."""
    match = re.search(rf'name="{name}" value="([^"]+)"', html)
    assert match, f"form field {name} not found in page"
    return match.group(1)


def receipt_order_id(html):
    match = re.search(r'data-testid="order-id">(\d+)<', html)
    return int(match.group(1)) if match else None


# ------------------------------------------------------------------ deploy
def run_deploy():
    """Run the project's own deploy script, like a developer would."""
    return subprocess.run([sys.executable, "deploy.py"], cwd=PROJECT_ROOT, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=1500)


def deployment_state():
    """{deployment: (image, rollout revision)} for the four app deployments."""
    data = json.loads(kubectl("get", "deployments", "-l", "app.kubernetes.io/component=app", "-o", "json").stdout)
    return {d["metadata"]["name"]: (d["spec"]["template"]["spec"]["containers"][0]["image"],
                                    d["metadata"]["annotations"].get("deployment.kubernetes.io/revision"))
            for d in data["items"]}
