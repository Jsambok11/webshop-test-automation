"""Unit tests for the frontend: product images, cart rules, checkout guards, delivery fees, rate
limiting and behaviour when a backend is down. The backend services are mocked, the session
cookie is read and written directly. CSRF is off here (tested at integration
level); rate limiting is switched on only in its own tests."""
import re
from html.parser import HTMLParser

import pytest
import requests

from support import (SECRET, flashed, get_session, logged_in_with_cart, placed_order, product,
                     response, set_session)

UNAVAILABLE = "The service is temporarily unavailable. Please try again later."


def products_answer(frontend, catalogue):
    """Make the mocked product-service answer GET /products (the whole catalogue)
    and GET /products/{id} (404 for unknown ids)."""
    def get(url, **_):
        if url.rstrip("/").endswith("/products"):
            return response(200, list(catalogue.values()))
        pid = int(url.rstrip("/").rsplit("/", 1)[1])
        if pid in catalogue:
            return response(200, catalogue[pid])
        return response(404, {"error": "Product not found"})
    frontend.outgoing.get.side_effect = get


# ---------------------------------------------------------------- REQ-09
@pytest.mark.tc("UT-009")
def test_delete_account_without_being_logged_in(frontend):
    """UT-009"""
    # Given nobody is logged in
    # When the delete-account form is posted
    resp = frontend.api.post("/delete-account")

    # Then the visitor is sent to the login page with a message
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/login")
    assert "You must be logged in to delete your account." in flashed(frontend)
    # And user-service is never asked to delete anything
    frontend.outgoing.delete.assert_not_called()


# ---------------------------------------------------------------- REQ-14
@pytest.mark.tc("UT-011")
@pytest.mark.parametrize("quantity", ["abc", "0", "-3", "1001"])
def test_invalid_cart_quantity_is_rejected(frontend, quantity):
    """UT-011: not a number, zero, negative, above the 1000 limit."""
    resp = frontend.api.post("/add-to-cart/1", data={"quantity": quantity})

    # Back to the product page with a message, cart untouched
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/product/1")
    assert "Quantity must be a whole number between 1 and 1000." in flashed(frontend)
    assert get_session(frontend).get("cart", []) == []


@pytest.mark.tc("UT-012")
def test_adding_the_same_product_twice_adds_up(frontend):
    """UT-012"""
    # Given product 1 has 15 in stock
    products_answer(frontend, {1: product(1, stock=15)})

    # When it is added twice, 2 and then 3
    frontend.api.post("/add-to-cart/1", data={"quantity": "2"})
    frontend.api.post("/add-to-cart/1", data={"quantity": "3"})

    # Then the cart has one line with quantity 5
    assert get_session(frontend)["cart"] == [{"product_id": 1, "quantity": 5}]


@pytest.mark.tc("UT-013")
def test_product_that_no_longer_exists_is_removed_from_the_cart(frontend):
    """UT-013"""
    # Given the cart contains product 9, which product-service no longer knows
    set_session(frontend, cart=[{"product_id": 9, "quantity": 1}])
    products_answer(frontend, {})

    # When the cart is opened
    page = frontend.api.get("/cart").get_data(as_text=True)

    # Then the customer is told, and the product is gone from the cart
    assert "Product 9 is no longer available and was removed from your cart." in page
    assert get_session(frontend)["cart"] == []


# ---------------------------------------------------------------- REQ-15
@pytest.mark.tc("UT-014")
def test_quantity_above_stock_is_not_added(frontend):
    """UT-014"""
    # Given Gaming Laptop 15 has 15 in stock
    products_answer(frontend, {1: product(1, stock=15, name="Gaming Laptop 15")})

    # When the customer tries to add 16
    resp = frontend.api.post("/add-to-cart/1", data={"quantity": "16"})

    # Then nothing is added and the customer sees how many are left
    assert resp.headers["Location"].endswith("/product/1")
    assert "Only 15 of Gaming Laptop 15 in stock." in flashed(frontend)
    assert get_session(frontend).get("cart", []) == []


@pytest.mark.tc("UT-015")
@pytest.mark.parametrize("extra, accepted", [(6, False), (5, True)], ids=["total-16", "total-15"])
def test_cart_quantity_plus_new_quantity_at_the_stock_boundary(frontend, extra, accepted):
    """UT-015: what is already in the cart counts towards the stock limit."""
    # Given stock is 15 and the cart already holds 10
    products_answer(frontend, {1: product(1, stock=15, name="Gaming Laptop 15")})
    set_session(frontend, cart=[{"product_id": 1, "quantity": 10}])

    # When `extra` more are added
    frontend.api.post("/add-to-cart/1", data={"quantity": str(extra)})

    # Then 10 + 6 = 16 is refused, 10 + 5 = 15 (exactly the stock) is accepted
    expected = 10 + extra if accepted else 10
    assert get_session(frontend)["cart"] == [{"product_id": 1, "quantity": expected}]


# ---------------------------------------------------------------- REQ-18
@pytest.mark.tc("UT-016")
@pytest.mark.parametrize("cart, expected_total", [
    ([{"product_id": 5, "quantity": 3}], "29.85"),                                       # 3 x 9.95
    ([{"product_id": 1, "quantity": 1}, {"product_id": 3, "quantity": 2}], "12399.95"),  # 9999.95 + 2 x 1200
], ids=["3x9.95", "9999.95+2x1200"])
def test_cart_totals_are_exact_to_the_cent(frontend, cart, expected_total):
    """UT-016: these amounts give rounding errors with float arithmetic."""
    products_answer(frontend, {1: product(1, price="9999.95"), 3: product(3, price="1200.00"),
                               5: product(5, price="9.95")})
    set_session(frontend, cart=cart)

    page = frontend.api.get("/cart").get_data(as_text=True)

    assert f"Subtotal: ${expected_total}" in page


# ---------------------------------------------------------------- REQ-19
@pytest.mark.tc("UT-017")
def test_checkout_as_guest_redirects_to_login(frontend):
    """UT-017"""
    # Given a guest with something in the cart
    set_session(frontend, cart=[{"product_id": 5, "quantity": 1}])

    # When the guest checks out
    resp = frontend.api.post("/checkout", data={"post_code": "12345", "delivery_method": "door"})

    # Then they are sent to log in, and no order is attempted
    assert resp.headers["Location"].endswith("/login")
    assert "Please log in to place your order." in flashed(frontend)
    frontend.outgoing.post.assert_not_called()


# ---------------------------------------------------------------- REQ-20
@pytest.mark.tc("UT-018")
@pytest.mark.parametrize("post_code, accepted", [
    ("", False),             # empty
    ("1" * 11, False),       # one above the maximum
    ("1", True),             # minimum
    ("1" * 10, True),        # maximum
], ids=["empty", "11-chars", "1-char", "10-chars"])
def test_post_code_boundaries(frontend, post_code, accepted):
    """UT-018"""
    # Given a logged-in customer ready to check out
    form = logged_in_with_cart(frontend, [{"product_id": 5, "quantity": 2}])
    frontend.outgoing.post.return_value = response(201, placed_order())

    # When checking out with the given post code
    resp = frontend.api.post("/checkout", data={**form, "post_code": post_code})

    # Then only 1-10 characters lead to an order
    if accepted:
        assert resp.status_code == 200
        frontend.outgoing.post.assert_called_once()
    else:
        assert resp.headers["Location"].endswith("/cart")
        assert "Please enter a valid post code." in flashed(frontend)
        frontend.outgoing.post.assert_not_called()


@pytest.mark.tc("UT-019")
def test_invalid_delivery_method_is_rejected(frontend):
    """UT-019"""
    form = logged_in_with_cart(frontend, [{"product_id": 5, "quantity": 2}])

    resp = frontend.api.post("/checkout", data={**form, "delivery_method": "drone"})

    assert resp.headers["Location"].endswith("/cart")
    assert "Please choose a delivery method." in flashed(frontend)
    frontend.outgoing.post.assert_not_called()


# ---------------------------------------------------------------- REQ-24
@pytest.mark.tc("UT-029")
@pytest.mark.parametrize("token", ["wrong-token", None], ids=["wrong-token", "no-token"])
def test_checkout_with_wrong_or_missing_token(frontend, token):
    """UT-029: the checkout token proves the form came from the current cart page."""
    # Given a customer whose session expects a different token
    form = logged_in_with_cart(frontend, [{"product_id": 5, "quantity": 2}])
    if token is None:
        del form["checkout_token"]
    else:
        form["checkout_token"] = token

    # When the form is posted
    resp = frontend.api.post("/checkout", data=form)

    # Then the customer is sent back to review the cart, and no order is placed
    assert resp.headers["Location"].endswith("/cart")
    assert "Your cart has changed. Please review it and place the order again." in flashed(frontend)
    frontend.outgoing.post.assert_not_called()


@pytest.mark.tc("UT-029")
def test_checkout_with_empty_cart(frontend):
    """UT-029 (empty cart)"""
    form = logged_in_with_cart(frontend, cart=[])

    resp = frontend.api.post("/checkout", data=form)

    assert resp.headers["Location"].endswith("/cart")
    frontend.outgoing.post.assert_not_called()


# ---------------------------------------------------------------- REQ-25
@pytest.mark.tc("UT-030")
def test_successful_checkout_clears_cart_and_token(frontend):
    """UT-030"""
    # Given a customer ready to check out, and order-service accepts the order
    form = logged_in_with_cart(frontend, [{"product_id": 5, "quantity": 2}], token="checkout-token-1")
    frontend.outgoing.post.return_value = response(201, placed_order())

    # When the order is placed
    resp = frontend.api.post("/checkout", data=form)

    # Then the receipt is shown
    assert resp.status_code == 200
    assert "Thank You for Your Order!" in resp.get_data(as_text=True)
    # And the checkout token was sent as the Idempotency-Key
    assert frontend.outgoing.post.call_args.kwargs["headers"]["Idempotency-Key"] == "checkout-token-1"
    # And the cart and token are removed, so a new checkout needs a new token
    session = get_session(frontend)
    assert "cart" not in session
    assert "checkout_token" not in session


# ---------------------------------------------------------------- REQ-32
@pytest.mark.tc("UT-034")
def test_login_rate_limit(make_service):
    """UT-034"""
    # Given a frontend that allows 2 login attempts per minute
    frontend = make_service("frontend-service", RATE_LIMIT_ENABLED="true", AUTH_RATE_LIMIT="2 per minute")
    frontend.outgoing.post.return_value = response(401, {"error": "Invalid email or password"})

    # When someone tries to log in three times
    replies = [frontend.api.post("/login", data={"email": "a@b.c", "password": "guess"}) for _ in range(3)]

    # Then the third attempt is blocked with a clear message
    assert [r.status_code for r in replies] == [401, 401, 429]
    assert "Too many attempts. Please wait a minute and try again." in replies[2].get_data(as_text=True)


@pytest.mark.tc("UT-035")
def test_registration_rate_limit(make_service):
    """UT-035"""
    # Given a frontend that allows 2 registrations per minute
    frontend = make_service("frontend-service", RATE_LIMIT_ENABLED="true", AUTH_RATE_LIMIT="2 per minute")
    frontend.outgoing.post.return_value = response(409, {"error": "A user with that email already exists"})
    form = {"firstname": "A", "surname": "B", "email": "a@b.c", "password": "Secret123"}

    # When three registrations are sent
    replies = [frontend.api.post("/register", data=form) for _ in range(3)]

    # Then the third is blocked, and the register page (not the login page) says why
    assert replies[2].status_code == 429
    page = replies[2].get_data(as_text=True)
    assert "Too many attempts" in page
    assert "Create Account" in page


# ---------------------------------------------------------------- REQ-34
@pytest.mark.tc("UT-037")
def test_frontend_when_a_backend_is_unreachable(frontend):
    """UT-037: friendly messages, no exception text, nothing lost."""
    # Given every backend call fails with an exception carrying internal details
    frontend.outgoing.get.side_effect = requests.ConnectionError(SECRET)
    frontend.outgoing.post.side_effect = requests.ConnectionError(SECRET)

    # When the home page is opened, then products could not be loaded
    home = frontend.api.get("/")
    assert home.status_code == 200
    assert "Products could not be loaded right now." in home.get_data(as_text=True)

    # When a product page is opened, then a 503 page is shown
    product_page = frontend.api.get("/product/1")
    assert product_page.status_code == 503
    assert UNAVAILABLE in product_page.get_data(as_text=True)

    # When someone logs in, then the same 503 message is shown
    login = frontend.api.post("/login", data={"email": "a@b.c", "password": "Secret123"})
    assert login.status_code == 503
    assert UNAVAILABLE in login.get_data(as_text=True)

    # When a logged-in customer checks out, then they are back at the cart, cart kept
    form = logged_in_with_cart(frontend, [{"product_id": 5, "quantity": 2}])
    checkout = frontend.api.post("/checkout", data=form)
    assert checkout.headers["Location"].endswith("/cart")
    assert UNAVAILABLE in flashed(frontend)
    assert get_session(frontend)["cart"] == [{"product_id": 5, "quantity": 2}]

    # And no page reveals the exception text
    for page in (home, product_page, login, checkout):
        assert SECRET not in page.get_data(as_text=True)


# ---------------------------------------------------------------- REQ-42
def receipt_delivery_and_total(page):
    """The delivery fee and the total shown on a receipt page, e.g. ("5.00", "104.99")."""
    fee = re.search(r'<div class="delivery">.*?\$(\d+\.\d{2})\s*</div>', page, re.S)
    total = re.search(r'data-testid="order-total">([\d.]+)<', page)
    assert fee and total, "receipt has no delivery fee or total"
    return fee.group(1), total.group(1)


@pytest.mark.tc("UT-041")
@pytest.mark.parametrize("items_total, fee, total", [
    ("99.99", "5.00", "104.99"),     # one cent below the limit
    ("100.00", "0.00", "100.00"),    # on the limit: free
    ("100.01", "0.00", "100.01"),    # one cent above the limit
], ids=["99.99", "100.00", "100.01"])
def test_door_delivery_fee_at_the_100_limit(frontend, items_total, fee, total):
    """UT-041: $99.99 + $5.00 also shows that the fee itself does not count towards the limit."""
    # Given a customer ready to check out, and the order's items total
    form = logged_in_with_cart(frontend, [{"product_id": 1, "quantity": 1}])
    frontend.outgoing.post.return_value = response(201, placed_order(lines=(("Item", items_total, 1),)))

    # When the order is placed with Delivery to door
    resp = frontend.api.post("/checkout", data={**form, "delivery_method": "door"})

    # Then the receipt shows the delivery fee and total for that amount
    assert resp.status_code == 200
    assert receipt_delivery_and_total(resp.get_data(as_text=True)) == (fee, total)


@pytest.mark.tc("UT-042")
@pytest.mark.parametrize("items_total", ["99.99", "100.00"])
def test_pickup_is_free_on_both_sides_of_the_limit(frontend, items_total):
    """UT-042"""
    # Given a customer ready to check out, and the order's items total
    form = logged_in_with_cart(frontend, [{"product_id": 1, "quantity": 1}])
    frontend.outgoing.post.return_value = response(201, placed_order(lines=(("Item", items_total, 1),)))

    # When the order is placed with Pickup at store
    resp = frontend.api.post("/checkout", data={**form, "delivery_method": "pickup"})

    # Then delivery is free and the total is the items total
    assert resp.status_code == 200
    assert receipt_delivery_and_total(resp.get_data(as_text=True)) == ("0.00", items_total)


@pytest.mark.tc("UT-043")
@pytest.mark.parametrize("price, door_label", [
    ("99.99", "Delivery to door – $5.00"),
    ("100.00", "Delivery to door – Free"),
], ids=["99.99", "100.00"])
def test_cart_shows_the_delivery_fee_for_the_subtotal(frontend, price, door_label):
    """UT-043"""
    # Given a logged-in customer with one product of the given price in the cart
    products_answer(frontend, {1: product(1, price=price)})
    set_session(frontend, user_id=1, user_email="customer@example.com",
                cart=[{"product_id": 1, "quantity": 1}])

    # When the cart is opened
    page = frontend.api.get("/cart").get_data(as_text=True)

    # Then door delivery shows the fee for this subtotal, and pickup is free
    assert f"Subtotal: ${price}" in page
    assert door_label in page
    assert "Pickup at store – Free" in page


# ---------------------------------------------------------------- REQ-44, REQ-45
PLACEHOLDER = "/static/placeholder.svg"


def images(page):
    """The <img> tags of a page, each as a dict of its attributes."""
    found = []

    class ImageTags(HTMLParser):
        def handle_starttag(self, tag, attrs):
            if tag == "img":
                found.append(dict(attrs))

    ImageTags().feed(page)
    return found


@pytest.mark.tc("UT-045")
def test_product_page_shows_image_and_description(frontend):
    """UT-045"""
    # Given product-service returns a product with a description and an image
    products_answer(frontend, {1: product(1, name="Ear Plugs", description="Soft foam ear plugs.",
                                          image="ear-plugs.jpg")})

    # When the product page is opened
    page = frontend.api.get("/product/1").get_data(as_text=True)

    # Then the description is shown
    assert "Soft foam ear plugs." in page
    # And the page has the product's image, with the product name as alt text
    assert [(i["src"], i["alt"]) for i in images(page)] == [("/static/products/ear-plugs.jpg", "Ear Plugs")]


@pytest.mark.tc("UT-046")
@pytest.mark.parametrize("image", [None, ""], ids=["image null", "image empty"])
@pytest.mark.parametrize("path", ["/", "/product/1"], ids=["home page", "product page"])
def test_product_without_image_shows_the_placeholder(frontend, path, image):
    """UT-046"""
    # Given product-service returns a product without an image (null or '')
    products_answer(frontend, {1: product(1, image=image)})

    # When the home page (card) or the product page is opened
    resp = frontend.api.get(path)

    # Then the placeholder is shown, and no image has an empty address
    assert resp.status_code == 200
    assert [i["src"] for i in images(resp.get_data(as_text=True))] == [PLACEHOLDER]
