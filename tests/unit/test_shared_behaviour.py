"""Unit tests for behaviour every backend shares (services/common/servicekit.py):
the internal token, generic error responses, required settings, request IDs
and health endpoints."""
import re

import pytest

from support import AUTH, SECRET, load_service, user_body

BACKENDS = ["user-service", "product-service", "order-service"]


# ---------------------------------------------------------------- REQ-28
@pytest.mark.tc("UT-033")
@pytest.mark.parametrize("headers", [{"X-Internal-Token": "wrong"}, {}], ids=["wrong-token", "no-token"])
def test_token_check_in_isolation(product_service, headers):
    """UT-033"""
    resp = product_service.api.get("/products", headers=headers)

    assert resp.status_code == 401
    assert resp.get_json() == {"error": "Unauthorized"}
    # Rejected before the route handler runs: the database is never touched
    product_service.db.cursor.assert_not_called()


# ---------------------------------------------------------------- REQ-34
@pytest.mark.tc("UT-036")
@pytest.mark.parametrize("service, method, path, body", [
    ("user-service", "post", "/users", user_body()),
    ("user-service", "post", "/login", {"email": "a@b.c", "password": "Secret123"}),
    ("user-service", "get", "/users/1", None),
    ("user-service", "delete", "/users/1", None),
    ("product-service", "get", "/products", None),
    ("product-service", "get", "/products/1", None),
    ("product-service", "post", "/products/1/decrease-stock", {"quantity": 1}),
    ("order-service", "post", "/users/1/orders/anonymize", None),
], ids=lambda v: v if isinstance(v, str) and v.startswith("/") else None)
def test_database_error_gives_a_generic_message(make_service, service, method, path, body):
    """UT-036: one parametrized test covers the database-error path of every endpoint."""
    # Given the database raises an error whose text contains internal details
    svc = make_service(service)
    svc.db.cursor.side_effect = Exception(SECRET)

    # When a valid request is sent
    resp = getattr(svc.api, method)(path, json=body, headers=AUTH)

    # Then the client only gets a generic message
    assert resp.status_code == 500
    assert resp.get_json() == {"error": "Internal server error"}
    assert SECRET not in resp.get_data(as_text=True)


# ---------------------------------------------------------------- REQ-36
@pytest.mark.tc("UT-038")
@pytest.mark.parametrize("missing", ["INTERNAL_API_TOKEN", "DB_PASSWORD"])
def test_service_refuses_to_start_without_its_settings(monkeypatch, missing):
    """UT-038: fail at startup, not on the first request."""
    with pytest.raises(RuntimeError, match=f"Environment variable {missing} must be set"):
        load_service("user-service", monkeypatch, **{missing: None})


# ---------------------------------------------------------------- REQ-40
@pytest.mark.tc("UT-039")
def test_valid_request_id_is_reused(product_service):
    """UT-039: an ID from the calling service is kept, so logs can be joined."""
    resp = product_service.api.get("/live", headers={"X-Request-ID": "abc-123"})

    assert resp.headers["X-Request-ID"] == "abc-123"


@pytest.mark.tc("UT-039")
def test_invalid_request_id_is_replaced(product_service):
    """UT-039: arbitrary input is never written to the logs."""
    resp = product_service.api.get("/live", headers={"X-Request-ID": "bad id!"})

    assert re.fullmatch(r"[0-9a-f]{32}", resp.headers["X-Request-ID"])


# ---------------------------------------------------------------- REQ-41
@pytest.mark.tc("UT-040")
@pytest.mark.parametrize("service", BACKENDS)
def test_health_reports_an_unreachable_database(make_service, service):
    """UT-040: /health (readiness) depends on the database, /live (liveness) does not."""
    # Given the database cannot be reached
    svc = make_service(service)
    svc.db.is_healthy.side_effect = Exception(SECRET)

    # When Kubernetes calls both probes (no token needed)
    health = svc.api.get("/health")
    live = svc.api.get("/live")

    # Then readiness fails without details, liveness still succeeds
    assert health.status_code == 500
    assert health.get_json() == {"status": "unhealthy"}
    assert live.status_code == 200
    assert live.get_json() == {"status": "alive"}
