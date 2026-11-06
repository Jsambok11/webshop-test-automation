"""Fixtures shared by the integration and E2E tests (both need the deployed
system: python deploy.py). Loaded through tests/conftest.py; they only start
port-forwards when a test actually uses them.

Test data rules:
- every test creates its own users (new_user) and they are deleted afterwards
- stock is put at a known value with set_stock and restored afterwards
- tests check the resulting state (stock, orders), not only status codes
"""
import uuid

import pytest

from cluster import PortForwards, ResilientSession, Sql, kubectl, logged_in_cookie, read_secret

__all__ = ["forwards", "urls", "token", "http", "api", "sql", "new_user", "set_stock",
           "place_order", "orders_of", "shop"]


@pytest.fixture(scope="session")
def forwards():
    """Port-forwards to all services for the whole run."""
    if kubectl("get", "secret", "app-secrets", check=False).returncode != 0:
        raise RuntimeError("The system is not deployed. Run `python deploy.py` first.")
    pf = PortForwards()
    pf.start()
    yield pf
    pf.stop()


@pytest.fixture(scope="session")
def urls(forwards):
    """Base URLs: urls["user"], urls["product"], urls["order"], urls["frontend"]."""
    return forwards.urls


@pytest.fixture(scope="session")
def token():
    return read_secret("internal-api-token")


@pytest.fixture(scope="session")
def http():
    """HTTP session without the internal token (for testing that it is required)."""
    return ResilientSession()


@pytest.fixture(scope="session")
def api(token):
    """HTTP session that sends the internal token, like the services do."""
    session = ResilientSession()
    session.headers["X-Internal-Token"] = token
    return session


@pytest.fixture(scope="session")
def sql():
    return Sql()


@pytest.fixture
def new_user(api, urls):
    """Factory: new_user(password="...") creates a user with a unique email.
    All users created in a test are deleted afterwards."""
    created = []

    def make(**overrides):
        body = {"firstname": "Test", "surname": "User",
                "email": f"it-{uuid.uuid4().hex[:12]}@example.com", "password": "Secret123", **overrides}
        resp = api.post(f"{urls['user']}/users", json=body)
        assert resp.status_code == 201, resp.text
        user = {**resp.json(), "password": body["password"]}
        created.append(user["id"])
        return user

    yield make
    for user_id in created:
        api.delete(f"{urls['user']}/users/{user_id}")


@pytest.fixture
def set_stock(api, urls):
    """set_stock(product_id, n) puts the stock at exactly n; the original is
    restored after the test. set_stock.of(product_id) reads the current stock."""
    originals = {}

    def of(pid):
        return api.get(f"{urls['product']}/products/{pid}").json()["stock"]

    def set_to(pid, target):
        originals.setdefault(pid, of(pid))
        diff = of(pid) - target
        if diff > 0:
            api.post(f"{urls['product']}/products/{pid}/decrease-stock", json={"quantity": diff}).raise_for_status()
        elif diff < 0:
            api.post(f"{urls['product']}/products/{pid}/increase-stock", json={"quantity": -diff}).raise_for_status()

    set_to.of = of
    yield set_to
    for pid, value in originals.items():
        set_to(pid, value)


@pytest.fixture
def place_order(api, urls):
    """place_order(user_id, (product_id, qty), ..., key=None) -> response."""
    def place(user_id, *lines, key=None):
        headers = {"Idempotency-Key": key} if key else {}
        return api.post(f"{urls['order']}/orders", headers=headers, json={
            "userId": user_id, "items": [{"productId": p, "quantity": q} for p, q in lines]})
    return place


@pytest.fixture
def orders_of(api, urls):
    """orders_of(user_id) -> list of the user's orders."""
    return lambda user_id: api.get(f"{urls['order']}/orders", params={"userId": user_id}).json()


@pytest.fixture
def shop(urls):
    """Factory for browser-like sessions on the frontend.
    shop() is a guest; shop(user, cart=[...]) is already logged in."""
    def make(user=None, cart=None):
        session = ResilientSession()
        data = {}
        if user:
            data.update(user_id=user["id"], user_email=user["email"])
        if cart is not None:
            data["cart"] = [{"product_id": p, "quantity": q} for p, q in cart]
        if data:
            # requests files cookies from "localhost" under "localhost.local";
            # using the same domain lets the server's updated cookie replace this one.
            session.cookies.set("session", logged_in_cookie(data), domain="localhost.local", path="/")
        session.base = urls["frontend"]
        return session
    return make
