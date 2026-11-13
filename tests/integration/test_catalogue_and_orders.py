"""Integration tests for products, stock and orders: order-service, product-service
and user-service working together with the real database."""
import concurrent.futures
import re
import uuid

import pytest

from cluster import form_value, receipt_order_id


def stock_change(api, urls, pid, quantity, action="decrease-stock"):
    return api.post(f"{urls['product']}/products/{pid}/{action}", json={"quantity": quantity})


# ---------------------------------------------------------------- REQ-12
@pytest.mark.tc("IT-011")
def test_product_list(api, urls):
    """IT-011"""
    resp = api.get(f"{urls['product']}/products")

    assert resp.status_code == 200
    products = resp.json()
    assert [p["id"] for p in products] == sorted(p["id"] for p in products)
    for p in products:
        assert re.fullmatch(r"\d+\.\d{2}", p["price"]), p      # money as a string, 2 decimals
        assert isinstance(p["stock"], int) and p["stock"] >= 0


# ---------------------------------------------------------------- REQ-18
@pytest.mark.tc("IT-012")
def test_order_total_is_an_exact_decimal(new_user, set_stock, place_order):
    """IT-012: 9999.95 + 3 x 99.95 = 10299.80 exactly (floats give 10299.800000000001)."""
    user = new_user()
    set_stock(1, 5)
    set_stock(4, 10)

    resp = place_order(user["id"], (1, 1), (4, 3))

    assert resp.status_code == 201
    order = resp.json()
    assert [i["subtotal"] for i in order["items"]] == ["9999.95", "299.85"]
    assert order["total"] == "10299.80"


# ---------------------------------------------------------------- REQ-21
@pytest.mark.tc("IT-013")
def test_order_with_several_products_is_one_order(new_user, set_stock, place_order, orders_of):
    """IT-013"""
    # Given a customer and two products in stock
    user = new_user()
    set_stock(1, 5)
    set_stock(4, 10)

    # When one order with both products is placed
    resp = place_order(user["id"], (1, 1), (4, 2))

    # Then it is one order with two lines
    assert resp.status_code == 201
    assert len(resp.json()["items"]) == 2
    # And stock is taken for both lines
    assert set_stock.of(1) == 4
    assert set_stock.of(4) == 8
    # And the customer has exactly one order
    assert [o["id"] for o in orders_of(user["id"])] == [resp.json()["id"]]


@pytest.mark.tc("IT-014")
def test_order_for_an_unknown_user_or_product(new_user, set_stock, place_order, orders_of):
    """IT-014: order-service checks with the real user- and product-service."""
    user = new_user()
    set_stock(5, 10)

    # When the user does not exist
    no_user = place_order(999999, (5, 1))
    # When the product does not exist
    no_product = place_order(user["id"], (99999, 1))

    # Then both are rejected with a clear message
    assert no_user.status_code == 400
    assert no_user.json() == {"error": "User with id 999999 does not exist"}
    assert no_product.status_code == 400
    assert no_product.json() == {"error": "Product with id 99999 does not exist"}
    # And nothing changed
    assert set_stock.of(5) == 10
    assert orders_of(user["id"]) == []


@pytest.mark.tc("IT-015")
def test_order_lookup_errors(api, urls):
    """IT-015"""
    unknown = api.get(f"{urls['order']}/orders/99999999")
    no_user = api.get(f"{urls['order']}/orders")

    assert unknown.status_code == 404
    assert unknown.json() == {"error": "Order not found"}
    assert no_user.status_code == 400
    assert no_user.json() == {"error": "userId query parameter is required"}


# ---------------------------------------------------------------- REQ-22
@pytest.mark.tc("IT-016")
def test_out_of_stock_line_rejects_the_whole_order(new_user, set_stock, place_order, orders_of):
    """IT-016: all-or-nothing across two services."""
    # Given product 1 has 5 in stock and product 4 is sold out
    user = new_user()
    set_stock(1, 5)
    set_stock(4, 0)

    # When an order with both is placed
    resp = place_order(user["id"], (1, 2), (4, 1))

    # Then the whole order is rejected
    assert resp.status_code == 400
    assert "Insufficient stock" in resp.json()["error"]
    # And product 1's reservation was given back
    assert set_stock.of(1) == 5
    # And no order exists
    assert orders_of(user["id"]) == []


# ---------------------------------------------------------------- REQ-23
@pytest.mark.tc("IT-017")
def test_parallel_orders_cannot_oversell(api, urls, set_stock):
    """IT-017: 20 buyers for the last 10 items, all at the same moment."""
    # Given 10 items in stock
    set_stock(2, 10)

    # When 20 requests for 1 item arrive in parallel
    with concurrent.futures.ThreadPoolExecutor(20) as pool:
        codes = list(pool.map(lambda _: stock_change(api, urls, 2, 1).status_code, range(20)))

    # Then exactly 10 succeed and the stock ends at exactly 0
    assert codes.count(200) == 10
    assert codes.count(400) == 10
    assert set_stock.of(2) == 0


@pytest.mark.tc("IT-018")
def test_stock_decrease_boundary(api, urls, set_stock):
    """IT-018"""
    set_stock(2, 10)

    # One more than in stock: rejected, nothing taken
    over = stock_change(api, urls, 2, 11)
    assert over.status_code == 400
    assert over.json() == {"error": "Insufficient stock. Available: 10"}
    assert set_stock.of(2) == 10

    # Exactly what is in stock: accepted, stock becomes 0
    exact = stock_change(api, urls, 2, 10)
    assert exact.status_code == 200
    assert exact.json()["new_stock"] == 0


@pytest.mark.tc("IT-019")
def test_database_refuses_negative_stock(sql, set_stock):
    """IT-019: the CHECK constraint is the last line of defence."""
    set_stock(5, 10)

    rc, out = sql.run("UPDATE ProductsDB.dbo.Products SET Stock = -1 WHERE Id = 5")

    assert rc != 0
    assert "CHECK constraint" in out
    assert set_stock.of(5) == 10


# ---------------------------------------------------------------- REQ-24
@pytest.mark.tc("IT-020")
def test_same_idempotency_key_creates_one_order(new_user, set_stock, place_order, orders_of):
    """IT-020"""
    # Given a customer and 10 Ear Plugs in stock
    user = new_user()
    set_stock(5, 10)
    key = uuid.uuid4().hex

    # When the same order is sent twice with the same Idempotency-Key
    first = place_order(user["id"], (5, 2), key=key)
    second = place_order(user["id"], (5, 2), key=key)

    # Then the second request returns the first order instead of a new one
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    # And stock was taken once
    assert set_stock.of(5) == 8
    assert len(orders_of(user["id"])) == 1


@pytest.mark.tc("IT-021")
def test_parallel_requests_with_the_same_key(new_user, set_stock, place_order, orders_of):
    """IT-021: the database's unique index decides the race."""
    user = new_user()
    set_stock(5, 10)
    key = uuid.uuid4().hex

    with concurrent.futures.ThreadPoolExecutor(5) as pool:
        replies = list(pool.map(lambda _: place_order(user["id"], (5, 2), key=key), range(5)))

    assert all(r.status_code in (200, 201) for r in replies), [r.status_code for r in replies]
    assert len({r.json()["id"] for r in replies}) == 1
    assert set_stock.of(5) == 8
    assert len(orders_of(user["id"])) == 1


@pytest.mark.tc("IT-022")
def test_key_reused_by_another_user(new_user, set_stock, place_order):
    """IT-022"""
    alice, bob = new_user(), new_user()
    set_stock(5, 10)
    key = uuid.uuid4().hex
    assert place_order(alice["id"], (5, 1), key=key).status_code == 201

    resp = place_order(bob["id"], (5, 1), key=key)

    assert resp.status_code == 409
    assert resp.json() == {"error": "Idempotency-Key was already used for another order"}


@pytest.mark.tc("IT-023")
def test_resent_checkout_form_shows_the_same_order(new_user, set_stock, shop, orders_of):
    """IT-023: a browser re-sending the form (double-click, back + resubmit)
    still sends the old session and the same checkout token."""
    # Given a logged-in customer with Gaming Laptop 15 in the cart
    user = new_user()
    set_stock(1, 5)
    browser = shop(user, cart=[(1, 1)])
    cart_page = browser.get(f"{browser.base}/cart").text
    form = {"csrf_token": form_value(cart_page, "csrf_token"),
            "checkout_token": form_value(cart_page, "checkout_token"),
            "post_code": "12345", "delivery_method": "door"}
    old_cookies = browser.cookies.copy()

    # When the checkout is submitted ...
    first = browser.post(f"{browser.base}/checkout", data=form)
    # ... and the same form is submitted again with the old session
    browser.cookies = old_cookies
    second = browser.post(f"{browser.base}/checkout", data=form)

    # Then both show a receipt for the same order
    assert first.status_code == second.status_code == 200
    assert receipt_order_id(first.text) is not None
    assert receipt_order_id(first.text) == receipt_order_id(second.text)
    # And the product was only bought once
    assert set_stock.of(1) == 4
    assert len(orders_of(user["id"])) == 1


# ---------------------------------------------------------------- REQ-26
@pytest.mark.tc("IT-024")
def test_old_order_keeps_its_price(api, urls, sql, new_user, set_stock, place_order):
    """IT-024: the order stores name and price at purchase time."""
    # Given an order of Ear Plugs at the current price
    user = new_user()
    set_stock(5, 10)
    price_then = api.get(f"{urls['product']}/products/5").json()["price"]
    order = place_order(user["id"], (5, 2)).json()

    try:
        # When the product's price is changed afterwards
        rc, out = sql.run("UPDATE ProductsDB.dbo.Products SET Price = 1.00 WHERE Id = 5")
        assert rc == 0, out

        # Then the old order still shows what the customer paid
        again = api.get(f"{urls['order']}/orders/{order['id']}").json()
        assert again["items"][0]["unitPrice"] == price_then
        assert again["items"][0]["productName"] == order["items"][0]["productName"]
        assert again["total"] == order["total"]
    finally:
        sql.run(f"UPDATE ProductsDB.dbo.Products SET Price = {price_then} WHERE Id = 5")


# ---------------------------------------------------------------- REQ-46
ALL_PRODUCTS = 20


@pytest.mark.tc("IT-042")
def test_product_api_lists_20_complete_products(api, urls):
    """IT-042"""
    # When the product list is requested
    resp = api.get(f"{urls['product']}/products")

    # Then there are exactly 20 products, ordered by id
    assert resp.status_code == 200
    products = resp.json()
    assert len(products) == ALL_PRODUCTS
    assert [p["id"] for p in products] == sorted(p["id"] for p in products)
    # And every product has a description and an image
    for p in products:
        assert p["description"] and p["description"].strip(), p
        assert p["image"] and p["image"].strip(), p


@pytest.mark.tc("IT-043")
def test_every_product_image_can_be_loaded(api, http, urls):
    """IT-043: through the frontend, as a browser loads them (no token needed)."""
    products = api.get(f"{urls['product']}/products").json()
    assert products

    for p in products:
        # Then no product uses the placeholder
        assert p["image"], f"{p['name']} has no image"
        # And its image answers 200 with an image content type
        resp = http.get(f"{urls['frontend']}/static/products/{p['image']}")
        assert resp.status_code == 200, f"{p['name']}: {p['image']} answered {resp.status_code}"
        assert resp.headers["Content-Type"].startswith("image/"), (p["image"], resp.headers["Content-Type"])
