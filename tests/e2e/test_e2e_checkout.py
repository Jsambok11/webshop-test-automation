"""E2E tests for checkout: placing orders and what the customer sees."""
import re

import pytest
from playwright.sync_api import Page, expect

from shop_ui import add_to_cart, cart_link, check_out, start_checkout, url_ending

EAR_PLUGS, HEADPHONES, POWERBANK = 5, 3, 4   # product ids, used by the test helpers
THANK_YOU = "Thank You for Your Order!"


# ---------------------------------------------------------------- REQ-19
@pytest.mark.tc("E2E-015")
def test_check_out_without_logging_in(page: Page, set_stock):
    """E2E-015"""
    # Given a visitor who is not logged in
    set_stock(EAR_PLUGS, 100)

    # When they add Ear Plugs, open Cart, enter post code 12345 and click Place Order
    add_to_cart(page, "Ear Plugs", 1)
    check_out(page, "12345")

    # Then the login page opens with a message
    expect(page).to_have_url(url_ending("/login"))
    expect(page.get_by_text("Please log in to place your order.")).to_be_visible()
    # And no order was placed (no stock was taken)
    assert set_stock.of(EAR_PLUGS) == 100


# ---------------------------------------------------------------- REQ-20
@pytest.mark.tc("E2E-016")
def test_delivery_fee_on_the_receipt(page: Page, new_user, set_stock, start_logged_in):
    """E2E-016"""
    # Given a logged-in customer and Ear Plugs in stock (test helper)
    start_logged_in(new_user())
    set_stock(EAR_PLUGS, 100)

    # When they buy 2 Ear Plugs with Pickup at store
    add_to_cart(page, "Ear Plugs", 2)
    check_out(page, delivery="pickup")
    # Then the receipt shows delivery $0.00 and total $19.90
    expect(page.locator(".delivery")).to_contain_text("Pickup at store")
    expect(page.locator(".delivery")).to_contain_text("$0.00")
    expect(page.get_by_test_id("order-total")).to_have_text("19.90")

    # When they buy 2 Ear Plugs again with Delivery to door
    add_to_cart(page, "Ear Plugs", 2)
    check_out(page, delivery="door")
    # Then the receipt shows delivery $5.00 and total $24.90
    expect(page.locator(".delivery")).to_contain_text("Delivery to door")
    expect(page.locator(".delivery")).to_contain_text("$5.00")
    expect(page.get_by_test_id("order-total")).to_have_text("24.90")


# ---------------------------------------------------------------- REQ-21
@pytest.mark.tc("E2E-017")
def test_buy_two_different_products(page: Page, new_user, set_stock, start_logged_in, orders_of):
    """E2E-017"""
    # Given a logged-in customer (test helper)
    customer = new_user()
    start_logged_in(customer)
    set_stock(HEADPHONES, 100)
    set_stock(EAR_PLUGS, 100)

    # When they add Noise-Cancelling Headphones and Ear Plugs, enter a post code and click Place Order
    add_to_cart(page, "Noise-Cancelling Headphones", 1)
    add_to_cart(page, "Ear Plugs", 1)
    check_out(page)

    # Then the receipt shows ONE order number with both products
    expect(page.get_by_test_id("order-id")).to_have_count(1)
    expect(page.locator(".order-item", has_text="Noise-Cancelling Headphones")).to_be_visible()
    expect(page.locator(".order-item", has_text="Ear Plugs")).to_be_visible()
    # And the shop has exactly one order for the customer (test helper)
    assert len(orders_of(customer["id"])) == 1


# ---------------------------------------------------------------- REQ-22
@pytest.mark.tc("E2E-018")
def test_product_sells_out_before_you_check_out(page: Page, new_user, set_stock, start_logged_in, orders_of):
    """E2E-018"""
    # Given a logged-in customer (test helper)
    customer = new_user()
    start_logged_in(customer)
    set_stock(HEADPHONES, 10)

    # When they add 2 Noise-Cancelling Headphones to the cart
    add_to_cart(page, "Noise-Cancelling Headphones", 2)
    # And the stock drops to 1 before they check out (test helper)
    set_stock(HEADPHONES, 1)
    # And they open Cart, enter a post code and click Place Order
    check_out(page)

    # Then they are told why, and that nothing was charged
    expect(page.get_by_text("Order not placed, nothing was charged: "
                            "Noise-Cancelling Headphones: Insufficient stock. Available: 1")).to_be_visible()
    # And Noise-Cancelling Headphones is still in the cart
    expect(page.locator(".cart-item", has_text="Noise-Cancelling Headphones")).to_be_visible()
    # And no receipt is shown, and no order exists (test helper)
    expect(page.get_by_text(THANK_YOU)).to_have_count(0)
    assert orders_of(customer["id"]) == []


# ---------------------------------------------------------------- REQ-24
@pytest.mark.tc("E2E-019")
def test_double_click_place_order(page: Page, new_user, set_stock, start_logged_in, orders_of):
    """E2E-019"""
    # Given a logged-in customer with Ear Plugs in the cart (test helper)
    customer = new_user()
    set_stock(EAR_PLUGS, 100)
    start_logged_in(customer, cart=[(EAR_PLUGS, 1)])

    # When they open Cart, enter a post code and double-click Place Order
    start_checkout(page)
    page.get_by_role("button", name="Place Order").dblclick()

    # Then one receipt is shown
    expect(page.get_by_text(THANK_YOU)).to_be_visible()
    # And only one order exists for the account (test helper)
    assert len(orders_of(customer["id"])) == 1
    assert set_stock.of(EAR_PLUGS) == 99


# ---------------------------------------------------------------- REQ-25
@pytest.mark.tc("E2E-020")
def test_receipt_after_buying(page: Page, new_user, set_stock, start_logged_in):
    """E2E-020"""
    # Given a logged-in customer and Ear Plugs in stock (test helper)
    start_logged_in(new_user())
    set_stock(EAR_PLUGS, 100)

    # When they add 2 Ear Plugs, enter post code 12345, keep Delivery to door
    # and click Place Order
    add_to_cart(page, "Ear Plugs", 2)
    check_out(page, "12345", "door")

    # Then the receipt thanks them and shows an order number
    expect(page.get_by_role("heading", name=THANK_YOU)).to_be_visible()
    expect(page.get_by_test_id("order-id")).to_have_text(re.compile(r"^\d+$"))
    # And the line Ear Plugs: $9.95 x 2 = $19.90
    expect(page.locator(".order-item", has_text="Ear Plugs")).to_contain_text("$9.95 × 2 = $19.90")
    # And delivery to the door $5.00, so the total is $24.90
    expect(page.locator(".delivery")).to_contain_text("$5.00")
    expect(page.get_by_test_id("order-total")).to_have_text("24.90")
    # And the cart is empty again
    expect(cart_link(page, 0)).to_be_visible()


# ---------------------------------------------------------------- REQ-42
@pytest.mark.tc("E2E-022")
def test_free_delivery_from_100(page: Page, new_user, set_stock, start_logged_in):
    """E2E-022"""
    # Given a logged-in customer, Powerbank 5000mah and Ear Plugs in stock (test helper)
    start_logged_in(new_user())
    set_stock(POWERBANK, 100)
    set_stock(EAR_PLUGS, 100)
    door = page.locator("label[for=door]")

    # When they add 1 Powerbank 5000mah ($99.95) and open Cart
    add_to_cart(page, "Powerbank 5000mah", 1)
    page.goto("/cart")
    # Then door delivery still costs $5.00
    expect(page.locator(".total")).to_have_text("Subtotal: $99.95")
    expect(door).to_have_text("Delivery to door – $5.00")

    # When they add 1 Ear Plugs ($9.95) and open Cart again
    add_to_cart(page, "Ear Plugs", 1)
    page.goto("/cart")
    # Then door delivery is free
    expect(page.locator(".total")).to_have_text("Subtotal: $109.90")
    expect(door).to_have_text("Delivery to door – Free")

    # When they place the order with Delivery to door
    check_out(page, "12345", "door")
    # Then the receipt shows delivery $0.00 and total $109.90
    expect(page.locator(".delivery")).to_contain_text("Delivery to door")
    expect(page.locator(".delivery")).to_contain_text("$0.00")
    expect(page.get_by_test_id("order-total")).to_have_text("109.90")
