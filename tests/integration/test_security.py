"""Integration tests for security: internal token, NetworkPolicies, database
permissions, CSRF, rate limiting, error details and security headers."""
import uuid

import pytest

from cluster import BACKENDS, form_value, kubectl, read_secret

# One harmless request per backend endpoint (safe to send with a valid token).
BACKEND_ENDPOINTS = [
    ("user", "post", "/users", {}),
    ("user", "post", "/login", {}),
    ("user", "get", "/users/1", None),
    ("user", "delete", "/users/999999999", None),
    ("product", "get", "/products", None),
    ("product", "get", "/products/1", None),
    ("product", "post", "/products/999999/decrease-stock", {"quantity": 1}),
    ("product", "post", "/products/999999/increase-stock", {"quantity": 1}),
    ("order", "post", "/orders", {}),
    ("order", "get", "/orders/999999999", None),
    ("order", "get", "/orders?userId=999999999", None),
    ("order", "post", "/users/999999999/orders/anonymize", None),
]
ENDPOINT_IDS = [f"{svc}-{method}-{path}" for svc, method, path, _ in BACKEND_ENDPOINTS]
LEAKS = ["Traceback", "pyodbc", "SQL", 'File "', "/app/"]


# ---------------------------------------------------------------- REQ-28
@pytest.mark.tc("IT-025")
@pytest.mark.parametrize("service, method, path, body", BACKEND_ENDPOINTS, ids=ENDPOINT_IDS)
def test_backend_apis_reject_missing_or_wrong_token(http, urls, token, service, method, path, body):
    """IT-025"""
    url = f"{urls[service]}{path}"
    send = getattr(http, method)

    # Without a token and with a wrong token: 401
    for headers in ({}, {"X-Internal-Token": "wrong-token"}):
        resp = send(url, json=body, headers=headers, timeout=10)
        assert resp.status_code == 401
        assert resp.json() == {"error": "Unauthorized"}

    # With the real token the request gets past the check
    assert send(url, json=body, headers={"X-Internal-Token": token}, timeout=10).status_code != 401


@pytest.mark.tc("IT-026")
@pytest.mark.parametrize("service", ["user", "product", "order"])
@pytest.mark.parametrize("probe", ["/health", "/live"])
def test_probe_endpoints_work_without_token(http, urls, service, probe):
    """IT-026: Kubernetes calls these without a token."""
    assert http.get(f"{urls[service]}{probe}", timeout=10).status_code == 200


# ---------------------------------------------------------------- REQ-29
@pytest.fixture
def probe_pod():
    """A pod without any app label, i.e. a random workload in the cluster."""
    name = f"np-probe-{uuid.uuid4().hex[:6]}"
    kubectl("run", name, "--image=curlimages/curl", "--restart=Never", "--command", "--", "sleep", "300")
    kubectl("wait", "--for=condition=ready", f"pod/{name}", "--timeout=120s", timeout=150)
    yield name
    kubectl("delete", "pod", name, "--wait=false", check=False)


def curl_status(pod, url):
    """HTTP status from inside a pod, or '000' when no connection was possible."""
    return kubectl("exec", pod, "--", "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-m", "4", url,
                   check=False).stdout.strip()


@pytest.mark.tc("IT-027")
def test_unauthorized_pod_is_blocked(probe_pod):
    """IT-027"""
    # The frontend is public
    assert curl_status(probe_pod, "http://frontend-service/live") == "200"
    # The backends only accept the services that need them
    for service in BACKENDS:
        assert curl_status(probe_pod, f"http://{service}/live") == "000", service
    # And the database is closed
    tcp = kubectl("exec", probe_pod, "--", "curl", "-sv", "-m", "4", "telnet://mssql-service:1433", check=False)
    assert "Connected to" not in tcp.stderr


PYTHON_GET = (
    "import sys, urllib.request\n"
    "for url in sys.argv[1:]:\n"
    "    try:\n"
    "        print(url, urllib.request.urlopen(url, timeout=4).status)\n"
    "    except Exception as exc:\n"
    "        print(url, 'blocked', type(exc).__name__)\n"
)


def requests_from(deployment, *targets):
    out = kubectl("exec", f"deploy/{deployment}", "--", "python", "-c", PYTHON_GET,
                  *[f"http://{t}/live" for t in targets]).stdout
    return {line.split()[0].split("/")[2]: line.split()[1] for line in out.splitlines()}


@pytest.mark.tc("IT-028")
def test_allowed_service_to_service_traffic():
    """IT-028: allowed paths work, a path the architecture does not need is blocked."""
    # order-service needs user-service and product-service
    assert requests_from("order-service", "user-service", "product-service") == {
        "user-service": "200", "product-service": "200"}
    # product-service never calls user-service
    assert requests_from("product-service", "user-service") == {"user-service": "blocked"}


# ---------------------------------------------------------------- REQ-30
@pytest.mark.tc("IT-029")
def test_product_service_db_login_has_minimum_rights(sql):
    """IT-029"""
    login = {"user": "product_svc", "password": read_secret("product-db-password"), "database": "ProductsDB"}

    # Reading products is allowed
    rc, out = sql.run("SELECT COUNT(*) FROM Products", **login)
    assert rc == 0, out
    # Deleting products is not
    rc, out = sql.run("DELETE FROM Products WHERE Id = -1", **login)
    assert rc != 0 and "permission was denied" in out
    # Other services' databases are not reachable
    rc, out = sql.run("SELECT COUNT(*) FROM UsersDB.dbo.Users", **login)
    assert rc != 0


@pytest.mark.tc("IT-030")
def test_user_and_order_db_logins_have_minimum_rights(sql):
    """IT-030"""
    order_login = {"user": "order_svc", "password": read_secret("order-db-password"), "database": "OrdersDB"}
    user_login = {"user": "user_svc", "password": read_secret("user-db-password"), "database": "UsersDB"}

    # order_svc may not delete orders (they are sales records) ...
    rc, out = sql.run("DELETE FROM Orders WHERE Id = -1", **order_login)
    assert rc != 0 and "permission was denied" in out
    # ... nor read products
    assert sql.run("SELECT COUNT(*) FROM ProductsDB.dbo.Products", **order_login)[0] != 0
    # user_svc may not read orders
    assert sql.run("SELECT COUNT(*) FROM OrdersDB.dbo.Orders", **user_login)[0] != 0


@pytest.mark.tc("IT-031")
def test_old_sa_password_does_not_work(sql):
    """IT-031: the password that used to be in the source code."""
    rc, out = sql.run("SELECT 1", user="sa", password="TestPass123!")

    assert rc != 0
    assert "Login failed" in out


# ---------------------------------------------------------------- REQ-31
@pytest.mark.tc("IT-032")
@pytest.mark.parametrize("path", ["/login", "/register", "/add-to-cart/1", "/checkout", "/logout", "/delete-account"])
def test_post_without_csrf_token_is_rejected(shop, path):
    """IT-032"""
    browser = shop()

    resp = browser.post(f"{browser.base}{path}", data={"email": "a@b.c", "password": "x", "quantity": "1"})

    assert resp.status_code == 400
    assert "Your form has expired or is invalid" in resp.text


@pytest.mark.tc("IT-033")
def test_state_changes_are_not_possible_with_get(shop, new_user):
    """IT-033: a link or <img> on another site must not log users out or change the cart."""
    # Given a logged-in customer with Gaming Laptop 15 in the cart
    user = new_user()
    browser = shop(user, cart=[(1, 1)])

    # When logout and remove are requested with GET
    assert browser.get(f"{browser.base}/logout").status_code == 405
    assert browser.get(f"{browser.base}/remove-from-cart/1").status_code == 405

    # Then the customer is still logged in and the cart is unchanged
    assert f"Welcome, {user['email']}" in browser.get(f"{browser.base}/").text
    assert "Gaming Laptop 15" in browser.get(f"{browser.base}/cart").text


# ---------------------------------------------------------------- REQ-32
@pytest.mark.tc("IT-034")
def test_login_rate_limit_in_the_deployed_system(shop):
    """IT-034: at most 5 attempts per minute.

    The counter lives in the frontend pod and cannot be reset from outside, so
    if this suite ran less than a minute ago the 429 can come earlier. The test
    therefore checks the rule itself: never more than 5 accepted attempts, and
    once blocked, always blocked."""
    browser = shop()
    csrf = form_value(browser.get(f"{browser.base}/login").text, "csrf_token")
    form = {"csrf_token": csrf, "email": f"nobody-{uuid.uuid4().hex[:6]}@example.com", "password": "guess"}

    codes = [browser.post(f"{browser.base}/login", data=form).status_code for _ in range(6)]

    assert 429 in codes
    assert codes.count(401) <= 5
    first_block = codes.index(429)
    assert all(c == 429 for c in codes[first_block:]), codes
    assert all(c == 401 for c in codes[:first_block]), codes


# ---------------------------------------------------------------- REQ-34
BAD_REQUESTS = [
    ("user", "post", "/users", {"data": "{not json", "headers": {"Content-Type": "application/json"}}),
    ("user", "post", "/users", {"json": {"firstname": 5, "surname": [], "email": {}, "password": None}}),
    ("user", "post", "/login", {"json": []}),
    ("user", "get", "/users/99999999", {}),
    ("product", "get", "/products/99999999", {}),
    ("product", "post", "/products/1/decrease-stock", {"json": {"quantity": "x"}}),
    ("product", "post", "/products/1/decrease-stock", {"data": "{not json", "headers": {"Content-Type": "application/json"}}),
    ("order", "post", "/orders", {"data": "{not json", "headers": {"Content-Type": "application/json"}}),
    ("order", "post", "/orders", {"json": {"userId": "x", "items": "y"}}),
    ("order", "get", "/orders/99999999", {}),
    ("order", "get", "/orders?userId=abc", {}),
]


@pytest.mark.tc("IT-035")
@pytest.mark.parametrize("service, method, path, kwargs", BAD_REQUESTS,
                         ids=[f"{s}-{m}-{p}-{i}" for i, (s, m, p, _) in enumerate(BAD_REQUESTS)])
def test_no_internal_details_in_error_responses(api, urls, service, method, path, kwargs):
    """IT-035"""
    resp = getattr(api, method)(f"{urls[service]}{path}", **kwargs)

    assert resp.status_code < 500, resp.text           # bad input is the client's fault
    for leak in LEAKS:
        assert leak not in resp.text, f"{leak!r} in {resp.text!r}"


# ---------------------------------------------------------------- REQ-35
@pytest.mark.tc("IT-036")
@pytest.mark.parametrize("page", ["/", "/cart", "/login", "/product/1"])
def test_security_headers_on_pages(http, urls, page):
    """IT-036"""
    headers = http.get(f"{urls['frontend']}{page}", timeout=10).headers

    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "same-origin"
