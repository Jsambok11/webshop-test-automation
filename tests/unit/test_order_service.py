"""Unit tests for order-service: input validation, merging of lines, the
all-or-nothing saga (compensation) and order limits.

user-service, product-service and the database are mocked, so failures can be
injected at exactly the moment a test needs (e.g. "line 2 fails")."""
import pytest
import requests

from support import AUTH, fake_dependencies, fake_saved_order, product, response, stock_calls

UNAVAILABLE = {"error": "A required service is unavailable, try again later"}


def order(*lines, user_id=1):
    """Request body for POST /orders. lines: (product_id, quantity) pairs."""
    return {"userId": user_id, "items": [{"productId": p, "quantity": q} for p, q in lines]}


# ---------------------------------------------------------------- REQ-21
@pytest.mark.tc("UT-020")
def test_duplicate_product_lines_are_merged(order_service):
    """UT-020"""
    # Given product 4 exists with enough stock
    fake_dependencies(order_service, products={4: product(4, name="Powerbank 5000mah", price="99.95")})
    fake_saved_order(order_service, lines=[(4, "Powerbank 5000mah", "99.95", 3)])

    # When the same product is sent in two lines (2 + 1)
    resp = order_service.api.post("/orders", json=order((4, 2), (4, 1)), headers=AUTH)

    # Then stock is reserved once for the merged quantity 3 ...
    assert resp.status_code == 201
    assert stock_calls(order_service, "decrease-stock") == [(4, 3)]
    # ... and one order line with quantity 3 is saved
    saved_lines = order_service.cursor.executemany.call_args.args[1]
    assert [(line[1], line[4]) for line in saved_lines] == [(4, 3)]


@pytest.mark.tc("UT-021")
@pytest.mark.parametrize("kwargs, expected_error", [
    ({"data": "not json", "content_type": "text/plain"}, "Request body must be a JSON object"),
    ({"json": {"items": [{"productId": 1, "quantity": 1}]}}, "Missing required fields: userId, productId, quantity"),
    ({"json": order((1, 1), user_id=0)}, "userId must be a positive integer"),
    ({"json": order((1, 1), user_id="1")}, "userId must be a positive integer"),
    ({"json": order((1, 1), user_id=True)}, "userId must be a positive integer"),
    ({"json": {"userId": 1, "items": ""}}, "items must be a non-empty list"),
    ({"json": {"userId": 1, "items": []}}, "items must be a non-empty list"),
    ({"json": {"userId": 1, "items": [5]}}, "Each item must be an object"),
    ({"json": order((0, 1))}, "productId must be a positive integer"),
], ids=["not-json", "no-userId", "userId-0", "userId-string", "userId-bool",
        "items-string", "items-empty", "item-not-object", "productId-0"])
def test_invalid_order_request_is_rejected(order_service, kwargs, expected_error):
    """UT-021: one test per validation rule; nothing may reach other services."""
    resp = order_service.api.post("/orders", headers=AUTH, **kwargs)

    assert resp.status_code == 400
    assert resp.get_json() == {"error": expected_error}
    order_service.outgoing.get.assert_not_called()
    order_service.outgoing.post.assert_not_called()


# ---------------------------------------------------------------- REQ-22
@pytest.mark.tc("UT-022")
def test_stock_released_when_a_later_line_fails(order_service):
    """UT-022: the core of the saga. Line 1 is reserved, line 2 fails,
    so line 1 must be given back and nothing saved."""
    # Given products 1 and 2 exist
    # And product 2 has no stock left
    fake_dependencies(
        order_service,
        products={1: product(1, name="P1"), 2: product(2, name="P2")},
        decrease={2: response(400, {"error": "Insufficient stock. Available: 0"})},
    )

    # When an order with both products is placed
    resp = order_service.api.post("/orders", json=order((1, 1), (2, 1)), headers=AUTH)

    # Then it is rejected with the name of the product that failed
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "P2: Insufficient stock. Available: 0"}
    # And the stock reserved for product 1 is released
    assert stock_calls(order_service, "increase-stock") == [(1, 1)]
    # And nothing is written to the database
    order_service.db.cursor.assert_not_called()


@pytest.mark.tc("UT-023")
def test_database_error_releases_all_reserved_stock(order_service):
    """UT-023"""
    # Given both lines can be reserved
    fake_dependencies(order_service, products={1: product(1), 2: product(2)})
    # But saving the order fails
    order_service.db.cursor.side_effect = Exception("database down")

    # When the order is placed
    resp = order_service.api.post("/orders", json=order((1, 1), (2, 2)), headers=AUTH)

    # Then the client gets a generic error and both reservations are undone
    assert resp.status_code == 500
    assert resp.get_json() == {"error": "Internal server error"}
    assert sorted(stock_calls(order_service, "increase-stock")) == [(1, 1), (2, 2)]


@pytest.mark.tc("UT-024")
def test_product_service_down_during_reservation(order_service):
    """UT-024"""
    # Given product-service goes down after the first reservation
    fake_dependencies(order_service, products={1: product(1), 2: product(2)},
                      decrease={2: requests.ConnectionError("product-service down")})

    # When the order is placed
    resp = order_service.api.post("/orders", json=order((1, 1), (2, 1)), headers=AUTH)

    # Then the client is told to retry later, and line 1 is released
    assert resp.status_code == 503
    assert resp.get_json() == UNAVAILABLE
    assert stock_calls(order_service, "increase-stock") == [(1, 1)]


@pytest.mark.tc("UT-025")
def test_user_service_down_while_validating_an_order(order_service):
    """UT-025"""
    # Given user-service cannot be reached
    order_service.outgoing.get.side_effect = requests.ConnectionError("user-service down")

    # When an order is placed
    resp = order_service.api.post("/orders", json=order((1, 1)), headers=AUTH)

    # Then it fails with 503 before any stock is touched
    assert resp.status_code == 503
    assert resp.get_json() == UNAVAILABLE
    order_service.outgoing.post.assert_not_called()


# ---------------------------------------------------------------- REQ-24
@pytest.mark.tc("UT-028")
@pytest.mark.parametrize("key, accepted", [
    ("x", False),               # 1 character
    ("a" * 7, False),           # one below the minimum
    ("a" * 8, True),            # minimum
    ("a" * 64, True),           # maximum
    ("a" * 65, False),          # one above the maximum
    ("has space1", False),      # forbidden character
], ids=["1-char", "7-chars", "8-chars", "64-chars", "65-chars", "space"])
def test_idempotency_key_format(order_service, key, accepted):
    """UT-028: boundary values on the 8-64 character rule."""
    fake_dependencies(order_service, products={1: product(1)})
    fake_saved_order(order_service, lines=[(1, "Product 1", "10.00", 1)], key_lookup=True)

    resp = order_service.api.post("/orders", json=order((1, 1)), headers={**AUTH, "Idempotency-Key": key})

    if accepted:
        assert resp.status_code == 201
    else:
        assert resp.status_code == 400
        assert resp.get_json() == {"error": "Idempotency-Key must be 8-64 letters, digits or dashes"}


# ---------------------------------------------------------------- REQ-27
@pytest.mark.tc("UT-031")
@pytest.mark.parametrize("lines, accepted", [
    ([(1, 1000)], True),            # maximum
    ([(1, 1001)], False),           # one above
    ([(1, 600), (1, 600)], False),  # each line is fine, the merged 1200 is not
], ids=["1000", "1001", "600+600"])
def test_quantity_limit_per_product(order_service, lines, accepted):
    """UT-031"""
    fake_dependencies(order_service, products={1: product(1, stock=5000)})
    fake_saved_order(order_service, lines=[(1, "Product 1", "10.00", 1000)])

    resp = order_service.api.post("/orders", json=order(*lines), headers=AUTH)

    if accepted:
        assert resp.status_code == 201
    else:
        assert resp.status_code == 400
        assert resp.get_json() == {"error": "Quantity must be an integer between 1 and 1000"}


@pytest.mark.tc("UT-032")
@pytest.mark.parametrize("count, accepted", [(50, True), (51, False)])
def test_maximum_different_products_per_order(order_service, count, accepted):
    """UT-032: at most 50 different products (the shop itself only has 6,
    which is why this boundary can only be tested at unit level)."""
    fake_dependencies(order_service, products={pid: product(pid) for pid in range(1, count + 1)})
    fake_saved_order(order_service)

    resp = order_service.api.post("/orders", json=order(*[(pid, 1) for pid in range(1, count + 1)]), headers=AUTH)

    if accepted:
        assert resp.status_code == 201
    else:
        assert resp.status_code == 400
        assert resp.get_json() == {"error": "An order can contain at most 50 different products"}
