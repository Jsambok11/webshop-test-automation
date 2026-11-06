"""Shared building blocks for all services in this project.

Copied into every image at build time (see the Dockerfiles), so each service
uses the same, tested implementation instead of four copy-pasted versions.

Contents:
- Request IDs: every request gets an X-Request-ID that is logged and passed on
  to downstream services, so one user action can be followed through all logs.
- Internal API auth: backend services require a shared token in the
  X-Internal-Token header (health endpoints excepted).
- HTTP client helper that adds the request ID and token to outgoing calls.
- Database helpers (connection per service user, cursor context manager).
- Small JSON helpers.
"""
import hmac
import logging
import os
import re
import uuid
from contextlib import contextmanager

import requests
from flask import g, has_request_context, jsonify, request

REQUEST_ID_HEADER = "X-Request-ID"
TOKEN_HEADER = "X-Internal-Token"
HTTP_TIMEOUT = 5
# Endpoints Kubernetes probes call; they never require the internal token.
PUBLIC_PATHS = {"/health", "/live"}
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")


def required_env(name):
    """Read a required setting. Fails at startup instead of on the first request."""
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Environment variable {name} must be set")
    return value


# ------------------- LOGGING + REQUEST IDS -------------------
class _RequestIdFilter(logging.Filter):
    def filter(self, record):
        record.request_id = g.get("request_id", "-") if has_request_context() else "-"
        return True


def current_request_id():
    return g.get("request_id") if has_request_context() else None


def setup_app(app, name):
    """Configure logging and request IDs for a Flask app. Returns its logger."""
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] [rid=%(request_id)s] %(message)s"))
    handler.addFilter(_RequestIdFilter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)

    @app.before_request
    def _assign_request_id():
        incoming = request.headers.get(REQUEST_ID_HEADER, "")
        # Only accept well-formed IDs from upstream; never log arbitrary input.
        g.request_id = incoming if _VALID_REQUEST_ID.match(incoming) else uuid.uuid4().hex

    @app.after_request
    def _return_request_id(response):
        response.headers[REQUEST_ID_HEADER] = g.get("request_id", "")
        return response

    @app.route("/live", methods=["GET"])
    def live():
        # Liveness: the process is up. Deliberately does NOT touch the database,
        # so a database outage does not make Kubernetes restart every service.
        return jsonify({"status": "alive"}), 200

    return logging.getLogger(name)


# ------------------- INTERNAL API AUTH -------------------
def require_internal_token(app, token):
    """Reject requests without the shared internal token (except probe endpoints)."""

    @app.before_request
    def _check_token():
        if request.path in PUBLIC_PATHS:
            return None
        sent = request.headers.get(TOKEN_HEADER, "")
        # compare_digest takes the same time whether the first or last character
        # differs, so the token cannot be guessed byte by byte from timings.
        if not hmac.compare_digest(sent.encode(), token.encode()):
            return jsonify({"error": "Unauthorized"}), 401
        return None


class ServiceClient:
    """HTTP client for calling other services: adds token, request ID, timeout."""

    def __init__(self, token, timeout=HTTP_TIMEOUT):
        self.token = token
        self.timeout = timeout
        self.session = requests.Session()

    def request(self, method, url, **kwargs):
        headers = kwargs.pop("headers", {}) or {}
        headers[TOKEN_HEADER] = self.token
        rid = current_request_id()
        if rid:
            headers[REQUEST_ID_HEADER] = rid
        kwargs.setdefault("timeout", self.timeout)
        return self.session.request(method, url, headers=headers, **kwargs)

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def delete(self, url, **kwargs):
        return self.request("DELETE", url, **kwargs)


def error_message(resp, fallback):
    """Pull the "error" message out of a service response, if there is one."""
    try:
        body = resp.json()
        if isinstance(body, dict) and body.get("error"):
            return body["error"]
    except ValueError:
        pass
    return fallback


# ------------------- DATABASE -------------------
class Database:
    """Connection settings for one service's own database login.

    Each service logs in with its own SQL user that can only touch its own
    tables (least privilege) instead of the all-powerful 'sa' account.
    """

    def __init__(self, server, database, user, password, trust_server_cert=True):
        self.conn_str = (
            "DRIVER={ODBC Driver 18 for SQL Server};"
            f"SERVER={server};DATABASE={database};UID={user};PWD={password};"
            "Encrypt=yes;"
            # SQL Server in this project uses a self-signed certificate. Traffic is
            # still encrypted, but the server's identity is not verified. With a
            # real certificate, set DB_TRUST_SERVER_CERT=no.
            f"TrustServerCertificate={'yes' if trust_server_cert else 'no'};"
        )

    @classmethod
    def from_env(cls, default_db):
        return cls(
            server=os.getenv("DB_SERVER", "mssql-service"),
            database=os.getenv("DB_NAME", default_db),
            user=required_env("DB_USER"),
            password=required_env("DB_PASSWORD"),
            trust_server_cert=os.getenv("DB_TRUST_SERVER_CERT", "yes").lower() == "yes",
        )

    def connect(self):
        import pyodbc  # imported here so the frontend image does not need the driver
        return pyodbc.connect(self.conn_str, timeout=5)

    @contextmanager
    def cursor(self):
        """Yield a cursor in one transaction: commit on success, roll back on error,
        always close the connection."""
        conn = self.connect()
        try:
            cur = conn.cursor()
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def is_healthy(self):
        with self.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()


# ------------------- JSON HELPERS -------------------
def error(message, status):
    return jsonify({"error": message}), status


def json_body():
    """The request body as a dict, or None if it is missing or not a JSON object."""
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else None


def is_positive_int(value):
    # bool is a subclass of int in Python, so True would otherwise count as 1.
    return isinstance(value, int) and not isinstance(value, bool) and value > 0
