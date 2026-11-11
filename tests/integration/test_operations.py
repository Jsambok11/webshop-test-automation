"""Integration tests for operations: request tracing, persistence, behaviour
during a database outage, repeatable deployment and upgrading from 1.1.

Tests marked `disruptive` stop the database or run deploy.py. They always run
last (see conftest.py). Skip them during development with:
    pytest tests/integration -m "not disruptive"
"""
import pytest

from cluster import BACKENDS, deployment_state, form_value, kubectl, pods, run_deploy, wait_until


def scale_database(replicas):
    kubectl("scale", "deployment/mssql-deployment", f"--replicas={replicas}")
    if replicas == 0:
        kubectl("wait", "--for=delete", "pod", "-l", "app=mssql", "--timeout=180s", check=False, timeout=200)
    else:
        kubectl("rollout", "status", "deployment/mssql-deployment", "--timeout=240s", timeout=260)


def wait_for_backends_ready(timeout=240):
    wait_until(lambda: all(p["ready"] for app in BACKENDS for p in pods(app)), timeout,
               "backends did not become ready again", interval=5)


# ---------------------------------------------------------------- REQ-40
@pytest.mark.tc("IT-041")
def test_request_id_appears_in_all_service_logs(new_user, set_stock, shop):
    """IT-041: one checkout can be followed through all four services."""
    # Given a logged-in customer with Ear Plugs in the cart
    user = new_user()
    set_stock(5, 10)
    browser = shop(user, cart=[(5, 1)])
    cart_page = browser.get(f"{browser.base}/cart").text

    # When the customer checks out
    resp = browser.post(f"{browser.base}/checkout", data={
        "csrf_token": form_value(cart_page, "csrf_token"),
        "checkout_token": form_value(cart_page, "checkout_token"),
        "post_code": "12345", "delivery_method": "door"})
    assert resp.status_code == 200
    request_id = resp.headers["X-Request-ID"]

    # Then the same request ID shows up in the logs of every service
    def services_that_logged_it():
        found = set()
        for app in ["frontend-service", *BACKENDS]:
            logs = kubectl("logs", "-l", f"app={app}", "--tail=3000", "--max-log-requests=10").stdout
            if request_id in logs:
                found.add(app)
        return found if len(found) == 4 else None

    wait_until(services_that_logged_it, 30, f"request id {request_id} not in all service logs")


# ---------------------------------------------------------------- REQ-37
@pytest.mark.tc("IT-037")
@pytest.mark.disruptive
def test_order_survives_a_database_restart(api, urls, new_user, set_stock, place_order):
    """IT-037"""
    # Given a customer with an order
    user = new_user()
    set_stock(5, 10)
    order = place_order(user["id"], (5, 1)).json()

    # When the database pod is stopped and started again
    try:
        scale_database(0)
    finally:
        scale_database(1)
    wait_for_backends_ready()

    # Then the user and the order are still there
    assert api.get(f"{urls['user']}/users/{user['id']}").status_code == 200
    again = api.get(f"{urls['order']}/orders/{order['id']}")
    assert again.status_code == 200
    assert again.json()["items"] == order["items"]


# ---------------------------------------------------------------- REQ-38
@pytest.mark.tc("IT-038")
@pytest.mark.disruptive
def test_database_outage_not_ready_and_not_restarted(api, urls, new_user, set_stock, place_order):
    """IT-038: readiness (needs the DB) fails, liveness (does not) keeps passing,
    so Kubernetes stops sending traffic but does not restart anything."""
    # Given the backends are running, and their restart counts are noted
    restarts_before = {p["name"]: p["restarts"] for app in BACKENDS for p in pods(app)}

    try:
        # When the database goes down
        scale_database(0)

        # Then every backend pod becomes NOT ready
        wait_until(lambda: not any(p["ready"] for app in BACKENDS for p in pods(app)), 120,
                   "backends still report ready without a database", interval=5)
        # And they are still alive (liveness does not depend on the database)
        for name in ("user", "product", "order"):
            assert api.get(f"{urls[name]}/live").status_code == 200
        # And none of them was restarted
        assert {p["name"]: p["restarts"] for app in BACKENDS for p in pods(app)} == restarts_before
    finally:
        # When the database comes back
        scale_database(1)

    # Then the backends recover by themselves and orders work again
    wait_for_backends_ready()
    user = new_user()
    set_stock(5, 10)
    assert place_order(user["id"], (5, 1)).status_code == 201


# ---------------------------------------------------------------- REQ-39
PRODUCT_COLUMNS_ADDED_IN_1_2 = ("Description", "ImageFile")


def make_products_table_look_like_1_1(sql):
    """Drop the columns that release 1.2 added, as on a database created by 1.1."""
    for column in PRODUCT_COLUMNS_ADDED_IN_1_2:
        rc, out = sql.run(f"IF COL_LENGTH('ProductsDB.dbo.Products', '{column}') IS NOT NULL "
                          f"ALTER TABLE ProductsDB.dbo.Products DROP COLUMN {column}")
        assert rc == 0, out


@pytest.fixture(scope="module")
def redeployed(api, urls, forwards, sql):
    """Runs deploy.py twice, like a developer re-running the deployment.

    Before the first run the database is put back in its 1.1 shape, so the
    first run is also an upgrade from 1.1 (IT-044). The state is recorded
    before and after the second run, which must change nothing."""
    # Given a user with an order
    email = "redeploy-check@example.com"
    user = api.post(f"{urls['user']}/users", json={"firstname": "Re", "surname": "Deploy",
                                                   "email": email, "password": "Secret123"}).json()
    if "id" not in user:   # left over from an interrupted earlier run
        user = api.post(f"{urls['user']}/login", json={"email": email, "password": "Secret123"}).json()
    order = api.post(f"{urls['order']}/orders", json={"userId": user["id"], "items": [{"productId": 5, "quantity": 1}]}).json()
    products_before = {p["id"]: p["stock"] for p in api.get(f"{urls['product']}/products").json()}

    # And a database as release 1.1 left it
    make_products_table_look_like_1_1(sql)

    # When deploy.py runs twice
    first = run_deploy()
    forwards.restart()          # port-forwards end when pods are replaced
    before = deployment_state()
    second = run_deploy()
    forwards.restart()
    after = deployment_state()

    yield {"user": user, "order": order, "runs": [first, second], "before": before, "after": after,
           "products_before": products_before}
    api.delete(f"{urls['user']}/users/{user['id']}")


@pytest.mark.tc("IT-039")
@pytest.mark.disruptive
def test_second_deploy_keeps_data(api, urls, redeployed):
    """IT-039"""
    for run in redeployed["runs"]:
        # Then each deploy finishes successfully and keeps the existing Secret
        assert run.returncode == 0, run.stdout[-3000:] + run.stderr[-3000:]
        assert "[OK] Deployment completed." in run.stdout
        assert "already exists, keeping it" in run.stdout

    # And the user and the order are still there
    assert api.get(f"{urls['user']}/users/{redeployed['user']['id']}").status_code == 200
    assert api.get(f"{urls['order']}/orders/{redeployed['order']['id']}").status_code == 200


@pytest.mark.tc("IT-040")
@pytest.mark.disruptive
def test_deploy_without_code_changes_changes_nothing(redeployed):
    """IT-040: the image tag is a hash of the source code, so the same code
    gives the same tag and Kubernetes has nothing to roll out."""
    assert redeployed["before"] == redeployed["after"]
    assert len(redeployed["after"]) == 4


# ---------------------------------------------------------------- REQ-46
@pytest.mark.tc("IT-044")
@pytest.mark.disruptive
def test_deploy_over_a_1_1_database(api, urls, redeployed):
    """IT-044: deploying 1.2 over a 1.1 database adds the new product fields
    and keeps the data. (The check for 20 products comes with issue #7.)"""
    # Then both deploys finished
    for run in redeployed["runs"]:
        assert run.returncode == 0, run.stdout[-3000:] + run.stderr[-3000:]

    # And every product has the description and image fields again
    resp = api.get(f"{urls['product']}/products")
    assert resp.status_code == 200
    products = resp.json()
    for p in products:
        assert "description" in p and "image" in p, p

    # And no product was lost, duplicated or had its stock changed
    assert {p["id"]: p["stock"] for p in products} == redeployed["products_before"]

    # And the test user's order is still there
    assert api.get(f"{urls['order']}/orders/{redeployed['order']['id']}").status_code == 200
