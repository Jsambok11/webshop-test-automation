"""E2E tests: Playwright drives a real browser (Chromium) through the shop.

Requires the deployed system (python deploy.py) and, once:
    pip install -r tests/requirements.txt
    playwright install chromium

Watch the browser while the tests run:
    pytest tests/e2e --headed --slowmo 300

The shared fixtures (new_user, set_stock, orders_of, ...) are the "test
helpers" of the E2E test cases: setup and checks done outside the browser.
"""
import pytest

from cluster import logged_in_cookie


@pytest.fixture(autouse=True)
def port_forwards_alive(forwards):
    """Before every test: restart any port-forward that kubectl has dropped."""
    forwards.ensure()


@pytest.fixture(scope="session")
def base_url(urls):
    """pytest-playwright uses this, so tests can write page.goto("/cart")."""
    return urls["frontend"]


@pytest.fixture
def start_logged_in(page, base_url):
    """Test helper for the prerequisite "Logged in" (and/or a filled cart):
    start_logged_in(user, cart=[(product_id, quantity)]).

    Puts a valid session cookie in the browser, signed with the frontend's own
    key. Tests whose subject is logging in use the real login form instead."""
    def start(user=None, cart=None):
        data = {}
        if user:
            data.update(user_id=user["id"], user_email=user["email"])
        if cart:
            data["cart"] = [{"product_id": p, "quantity": q} for p, q in cart]
        page.context.add_cookies([{"name": "session", "value": logged_in_cookie(data), "url": base_url}])
    return start


@pytest.fixture
def delete_afterwards(api, urls):
    """For accounts created through the register page: delete_afterwards(email, password)
    removes the account when the test ends (looked up through the login API)."""
    accounts = []
    yield lambda email, password: accounts.append((email, password))
    for email, password in accounts:
        resp = api.post(f"{urls['user']}/login", json={"email": email, "password": password})
        if resp.status_code == 200:
            api.delete(f"{urls['user']}/users/{resp.json()['id']}")
