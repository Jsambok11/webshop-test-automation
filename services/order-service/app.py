"""Order service: creates orders (with one line per product) for a user.

Creating an order is all-or-nothing across services (a "saga"):
  1. validate input, check the user and every product exist
  2. reserve stock for each line in product-service
  3. save the order and its lines in ONE database transaction
If any step fails, stock reserved in step 2 is given back (compensation), so a
checkout either fully succeeds or leaves nothing behind.

Idempotency: the client may send an Idempotency-Key header. Sending the same
key again (double-click, retry after a timeout) returns the order that was
already created instead of creating, and charging for, a second one.
"""
import os
import re
from decimal import Decimal

import pyodbc
import requests
from flask import Flask, jsonify, request

from servicekit import (Database, ServiceClient, error, error_message, is_positive_int,
                        json_body, required_env, require_internal_token, setup_app)

app = Flask(__name__)
log = setup_app(app, "order-service")

INTERNAL_TOKEN = required_env("INTERNAL_API_TOKEN")
require_internal_token(app, INTERNAL_TOKEN)
db = Database.from_env("OrdersDB")
client = ServiceClient(INTERNAL_TOKEN)

USER_SERVICE_URL = os.getenv("USER_SERVICE_URL", "http://user-service:80")
PRODUCT_SERVICE_URL = os.getenv("PRODUCT_SERVICE_URL", "http://product-service:80")
MAX_QUANTITY = 1000
MAX_LINES = 50
IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9-]{8,64}$")


class DependencyError(Exception):
    """A service we depend on could not be reached or answered unexpectedly."""


class OrderRejected(Exception):
    """The order cannot be placed (unknown product, not enough stock, ...)."""


# ------------------- CALLS TO OTHER SERVICES -------------------
def fetch_json(url):
    """GET a resource. Returns its JSON, None on 404, DependencyError otherwise."""
    try:
        resp = client.get(url)
    except requests.RequestException as exc:
        raise DependencyError(str(exc)) from exc
    if resp.status_code == 200:
        return resp.json()
    if resp.status_code == 404:
        return None
    raise DependencyError(f"{url} returned HTTP {resp.status_code}")


def reserve_stock(line):
    try:
        resp = client.post(f"{PRODUCT_SERVICE_URL}/products/{line['productId']}/decrease-stock",
                           json={"quantity": line["quantity"]})
    except requests.RequestException as exc:
        raise DependencyError(str(exc)) from exc
    if resp.status_code == 200:
        return
    if resp.status_code in (400, 404):
        raise OrderRejected(f"{line['productName']}: {error_message(resp, 'Not available')}")
    raise DependencyError(f"decrease-stock returned HTTP {resp.status_code}")


def release_stock(lines):
    """Compensating action: give back stock that was reserved for a failed order."""
    for line in lines:
        try:
            resp = client.post(f"{PRODUCT_SERVICE_URL}/products/{line['productId']}/increase-stock",
                               json={"quantity": line["quantity"]})
            if resp.status_code != 200:
                log.error("Stock release for product %s returned HTTP %s", line["productId"], resp.status_code)
        except requests.RequestException:
            # Nothing more we can do here; log loudly so it can be fixed by hand.
            log.exception("Could not release %s units of product %s", line["quantity"], line["productId"])


# ------------------- DATABASE HELPERS -------------------
def load_order(cur, order_id):
    cur.execute("SELECT Id, UserId, CreatedAt FROM Orders WHERE Id = ?", order_id)
    head = cur.fetchone()
    if head is None:
        return None
    cur.execute(
        "SELECT ProductId, ProductName, UnitPrice, Quantity FROM OrderItems WHERE OrderId = ? ORDER BY Id",
        order_id,
    )
    items = [
        {
            "productId": r[0],
            "productName": r[1],
            "unitPrice": f"{r[2]:.2f}",
            "quantity": r[3],
            "subtotal": f"{r[2] * r[3]:.2f}",
        }
        for r in cur.fetchall()
    ]
    total = sum((Decimal(i["subtotal"]) for i in items), Decimal("0"))
    return {
        "id": head[0],
        "userId": head[1],
        "createdAt": head[2].isoformat() if head[2] else None,
        "items": items,
        "total": f"{total:.2f}",
    }


def find_by_key(cur, key):
    cur.execute("SELECT Id, UserId FROM Orders WHERE IdempotencyKey = ?", key)
    return cur.fetchone()


def replay(key, user_id):
    """Response for a request whose Idempotency-Key was already used, else None."""
    with db.cursor() as cur:
        row = find_by_key(cur, key)
        if row is None:
            return None
        if row[1] != user_id:
            return error("Idempotency-Key was already used for another order", 409)
        log.info("Idempotent replay of order %s", row[0])
        return jsonify(load_order(cur, row[0])), 200


# ------------------- INPUT PARSING -------------------
def parse_lines(data):
    """Return a list of {"productId", "quantity"} or raise OrderRejected.

    Accepts {"items": [{"productId", "quantity"}, ...]} or, for single-product
    orders, {"productId", "quantity"}. Duplicate products are merged.
    """
    if "items" in data:
        raw = data["items"]
        if not isinstance(raw, list) or not raw:
            raise OrderRejected("items must be a non-empty list")
    elif data.get("productId") is not None or data.get("quantity") is not None:
        raw = [{"productId": data.get("productId"), "quantity": data.get("quantity")}]
    else:
        raise OrderRejected("Missing required fields: userId, productId, quantity")

    merged = {}
    for item in raw:
        if not isinstance(item, dict):
            raise OrderRejected("Each item must be an object")
        pid, qty = item.get("productId"), item.get("quantity")
        if pid is None or qty is None:
            raise OrderRejected("Missing required fields: userId, productId, quantity")
        if not is_positive_int(pid):
            raise OrderRejected("productId must be a positive integer")
        if not is_positive_int(qty):
            raise OrderRejected(f"Quantity must be an integer between 1 and {MAX_QUANTITY}")
        merged[pid] = merged.get(pid, 0) + qty

    if len(merged) > MAX_LINES:
        raise OrderRejected(f"An order can contain at most {MAX_LINES} different products")
    for qty in merged.values():
        if qty > MAX_QUANTITY:
            raise OrderRejected(f"Quantity must be an integer between 1 and {MAX_QUANTITY}")
    return [{"productId": pid, "quantity": qty} for pid, qty in merged.items()]


# ------------------- ENDPOINTS -------------------
@app.route("/health", methods=["GET"])
def health():
    """Readiness: the service can reach its database."""
    try:
        db.is_healthy()
        return jsonify({"status": "healthy"}), 200
    except Exception:
        log.exception("Health check failed")
        return jsonify({"status": "unhealthy"}), 500


@app.route("/orders", methods=["POST"])
def create_order():
    data = json_body()
    if data is None:
        return error("Request body must be a JSON object", 400)

    user_id = data.get("userId")
    if user_id is None:
        return error("Missing required fields: userId, productId, quantity", 400)
    if not is_positive_int(user_id):
        return error("userId must be a positive integer", 400)
    try:
        lines = parse_lines(data)
    except OrderRejected as exc:
        return error(str(exc), 400)

    key = request.headers.get("Idempotency-Key")
    if key is not None and not IDEMPOTENCY_KEY.match(key):
        return error("Idempotency-Key must be 8-64 letters, digits or dashes", 400)

    try:
        if key:
            replayed = replay(key, user_id)
            if replayed:
                return replayed

        # 1. Validate the user and products, and take a snapshot of name and price.
        if fetch_json(f"{USER_SERVICE_URL}/users/{user_id}") is None:
            return error(f"User with id {user_id} does not exist", 400)
        for line in lines:
            product = fetch_json(f"{PRODUCT_SERVICE_URL}/products/{line['productId']}")
            if product is None:
                return error(f"Product with id {line['productId']} does not exist", 400)
            line["productName"] = product["name"]
            line["unitPrice"] = Decimal(str(product["price"]))
    except DependencyError:
        log.exception("Dependency check failed")
        return error("A required service is unavailable, try again later", 503)
    except Exception:
        log.exception("Idempotency lookup failed")
        return error("Internal server error", 500)

    # 2. Reserve stock line by line; on any failure give back what was reserved.
    reserved = []
    try:
        for line in lines:
            reserve_stock(line)
            reserved.append(line)
    except OrderRejected as exc:
        release_stock(reserved)
        return error(str(exc), 400)
    except DependencyError:
        log.exception("Stock reservation failed")
        release_stock(reserved)
        return error("A required service is unavailable, try again later", 503)

    # 3. Save order + lines atomically.
    try:
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO Orders (UserId, IdempotencyKey) OUTPUT INSERTED.Id VALUES (?, ?)",
                (user_id, key),
            )
            order_id = cur.fetchone()[0]
            cur.executemany(
                "INSERT INTO OrderItems (OrderId, ProductId, ProductName, UnitPrice, Quantity) "
                "VALUES (?, ?, ?, ?, ?)",
                [(order_id, l["productId"], l["productName"], l["unitPrice"], l["quantity"]) for l in lines],
            )
            order = load_order(cur, order_id)
    except pyodbc.IntegrityError:
        # Same Idempotency-Key sent twice at the same moment: the other request
        # won the race. Undo our reservation and return the other order.
        release_stock(reserved)
        replayed = replay(key, user_id) if key else None
        return replayed or error("Order could not be saved", 409)
    except Exception:
        log.exception("Failed to save order; releasing stock")
        release_stock(reserved)
        return error("Internal server error", 500)

    log.info("Created order %s for user %s (%s lines)", order_id, user_id, len(lines))
    return jsonify(order), 201


@app.route("/orders/<int:order_id>", methods=["GET"])
def get_order(order_id):
    try:
        with db.cursor() as cur:
            order = load_order(cur, order_id)
    except Exception:
        log.exception("Failed to fetch order %s", order_id)
        return error("Internal server error", 500)
    if order is None:
        return error("Order not found", 404)
    return jsonify(order), 200


@app.route("/orders", methods=["GET"])
def list_orders():
    """GET /orders?userId=<id> - all orders for one user."""
    user_id = request.args.get("userId", type=int)
    if not user_id or user_id <= 0:
        return error("userId query parameter is required", 400)
    try:
        with db.cursor() as cur:
            cur.execute("SELECT Id FROM Orders WHERE UserId = ? ORDER BY Id", user_id)
            ids = [r[0] for r in cur.fetchall()]
            orders = [load_order(cur, i) for i in ids]
    except Exception:
        log.exception("Failed to list orders for user %s", user_id)
        return error("Internal server error", 500)
    return jsonify(orders), 200


@app.route("/users/<int:user_id>/orders/anonymize", methods=["POST"])
def anonymize_orders(user_id):
    """Called by user-service before a user is deleted. Orders are kept for
    bookkeeping, but no longer linked to the person."""
    try:
        with db.cursor() as cur:
            cur.execute("UPDATE Orders SET UserId = NULL WHERE UserId = ?", user_id)
            count = cur.rowcount
    except Exception:
        log.exception("Failed to anonymize orders for user %s", user_id)
        return error("Internal server error", 500)
    log.info("Anonymized %s orders of user %s", count, user_id)
    return jsonify({"anonymized": count}), 200


if __name__ == "__main__":
    # Local development only. In the container the app runs under gunicorn.
    app.run(host="0.0.0.0", port=5000, debug=os.getenv("FLASK_DEBUG") == "1")
