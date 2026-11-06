"""Helpers for unit tests: load a service in isolation and fake its surroundings.

A service (services/<name>/app.py) talks to the outside world through two
module-level objects:

    db      database access      ->  replaced by a MagicMock
    client  calls to other services  ->  replaced by a MagicMock

The service's own Flask app is then driven in-process with Flask's test
client, so no network, Docker or database is involved.
"""
import importlib.util
import sys
import types
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "common"))  # makes `import servicekit` work

TOKEN = "unit-test-token"
AUTH = {"X-Internal-Token": TOKEN}
SECRET = "SECRET-DETAIL-pwd=hunter2"   # text that must never leak into a response

DEFAULT_ENV = {
    "INTERNAL_API_TOKEN": TOKEN,
    "DB_USER": "unit",
    "DB_PASSWORD": "unit",
    "CSRF_ENABLED": "false",        # unit tests post forms directly
    "RATE_LIMIT_ENABLED": "false",  # switched on only in the rate-limit tests
}


@dataclass
class Service:
    """Everything a unit test needs to drive one service."""
    module: types.ModuleType   # the imported app.py
    api: object                # Flask test client: sends requests TO the service
    db: MagicMock              # fake database
    cursor: MagicMock          # what `with db.cursor() as cur` yields
    outgoing: MagicMock        # fake ServiceClient: requests FROM the service


def load_service(name, monkeypatch, **env):
    """Import services/<name>/app.py with test settings.

    Pass NAME=None to remove a setting (used to test missing configuration).
    """
    for key, value in {**DEFAULT_ENV, **env}.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    # pyodbc needs Microsoft's native ODBC driver. Unit tests never reach a
    # database, so a fake module with the one class the code uses is enough.
    fake_pyodbc = types.ModuleType("pyodbc")
    fake_pyodbc.IntegrityError = type("IntegrityError", (Exception,), {})
    monkeypatch.setitem(sys.modules, "pyodbc", fake_pyodbc)

    module_name = name.replace("-", "_")
    spec = importlib.util.spec_from_file_location(module_name, ROOT / "services" / name / "app.py")
    module = importlib.util.module_from_spec(spec)
    # Registered so Flask can find the frontend's templates/ folder.
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module


def start_service(name, monkeypatch, **env):
    """Load a service and replace its database and outgoing HTTP client."""
    module = load_service(name, monkeypatch, **env)
    db, outgoing = MagicMock(), MagicMock()
    if hasattr(module, "db"):
        module.db = db
    if hasattr(module, "client"):
        module.client = outgoing
    cursor = db.cursor.return_value.__enter__.return_value
    return Service(module, module.app.test_client(), db, cursor, outgoing)


def response(status, body=None):
    """A fake `requests.Response` as returned by the mocked ServiceClient."""
    resp = MagicMock(status_code=status)
    resp.json.return_value = {} if body is None else body
    return resp


def user_body(**overrides):
    """A valid registration request; override single fields per test."""
    return {"firstname": "Test", "surname": "User", "email": "test@example.com",
            "password": "Secret123", **overrides}


def product(pid, stock=100, price="10.00", name=None):
    return {"id": pid, "name": name or f"Product {pid}", "price": price, "stock": stock}


def sql_calls(cursor):
    """All SQL statements the fake cursor received."""
    calls = [c.args[0] for c in cursor.execute.call_args_list]
    calls += [c.args[0] for c in cursor.executemany.call_args_list]
    return calls


# ------------------------------------------------------------------ order-service
def fake_dependencies(svc, products=None, user_status=200, decrease=None):
    """Make the mocked user-service and product-service answer order-service.

    products  {product_id: product dict}; unknown ids answer 404
    decrease  {product_id: response or exception} for decrease-stock; default 200
    """
    products = products or {}
    decrease = decrease or {}

    def get(url, **_):
        if "/users/" in url:
            return response(user_status, {"id": 1} if user_status == 200 else {"error": "User not found"})
        pid = int(url.rstrip("/").rsplit("/", 1)[1])
        if pid in products:
            return response(200, products[pid])
        return response(404, {"error": "Product not found"})

    def post(url, **_):
        if url.endswith("/decrease-stock"):
            pid = int(url.split("/products/")[1].split("/")[0])
            result = decrease.get(pid, response(200, {"new_stock": 1}))
            if isinstance(result, Exception):
                raise result
            return result
        return response(200, {"new_stock": 1})   # increase-stock (compensation)

    svc.outgoing.get.side_effect = get
    svc.outgoing.post.side_effect = post


def fake_saved_order(svc, order_id=10, user_id=1, lines=(), key_lookup=False):
    """Make the fake database answer the INSERTs and the read-back of an order.

    lines      [(product_id, name, unit_price "9.95", quantity), ...]
    key_lookup True when the request has an Idempotency-Key (one extra lookup first)
    """
    answers = [None] if key_lookup else []            # key not used before
    answers += [(order_id,), (order_id, user_id, datetime(2026, 11, 20, 12, 0, 0))]
    svc.cursor.fetchone.side_effect = answers
    svc.cursor.fetchall.return_value = [(pid, name, Decimal(price), qty) for pid, name, price, qty in lines]


def stock_calls(svc, action):
    """(product_id, quantity) for every decrease-stock / increase-stock call made."""
    calls = []
    for c in svc.outgoing.post.call_args_list:
        url = c.args[0]
        if url.endswith(f"/{action}"):
            pid = int(url.split("/products/")[1].split("/")[0])
            calls.append((pid, c.kwargs["json"]["quantity"]))
    return calls


# ------------------------------------------------------------------ frontend
def set_session(svc, **values):
    """Write values into the frontend's session cookie (e.g. user_id, cart)."""
    with svc.api.session_transaction() as session:
        session.update(values)


def get_session(svc):
    with svc.api.session_transaction() as session:
        return dict(session)


def flashed(svc):
    """Messages flashed to the user and not yet displayed."""
    return [message for _category, message in get_session(svc).get("_flashes", [])]


def logged_in_with_cart(svc, cart, token="checkout-token-1"):
    """A logged-in customer with a cart and a checkout token, ready to check out."""
    set_session(svc, user_id=1, user_email="customer@example.com",
                cart=cart, checkout_token=token)
    return {"checkout_token": token, "post_code": "12345", "delivery_method": "door"}


def placed_order(order_id=7, lines=(("Ear Plugs", "9.95", 2),)):
    """The JSON order-service returns for a successful order."""
    items = [{"productId": i + 1, "productName": name, "unitPrice": price, "quantity": qty,
              "subtotal": f"{Decimal(price) * qty:.2f}"} for i, (name, price, qty) in enumerate(lines)]
    total = sum(Decimal(i["subtotal"]) for i in items)
    return {"id": order_id, "userId": 1, "createdAt": "2026-11-20T12:00:00", "items": items, "total": f"{total:.2f}"}
