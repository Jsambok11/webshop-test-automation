"""E2E tests for browsing products and using the cart."""
import re

import pytest
from playwright.sync_api import Page, expect

from shop_ui import (PLACEHOLDER_IMAGE, add_to_cart, cart_link, image_is_shown, log_in, open_product,
                     url_ending)

EAR_PLUGS, JBL, LENOVO = 5, 3, 2   # product ids, used by the test helpers


# ---------------------------------------------------------------- REQ-12
@pytest.mark.tc("E2E-008")
def test_see_all_products(page: Page):
    """E2E-008"""
    # When the visitor opens the home page
    page.goto("/")

    # Then all 6 products are listed
    cards = page.locator(".product-card")
    expect(cards).to_have_count(6)
    # And each shows a name, a price like $9999.95 and "Stock: <number>"
    for i in range(6):
        card = cards.nth(i)
        expect(card.locator(".product-name")).not_to_be_empty()
        expect(card.locator(".price")).to_have_text(re.compile(r"^\$\d+\.\d{2}$"))
        expect(card.locator(".stock")).to_have_text(re.compile(r"Stock: \d+"))


# ---------------------------------------------------------------- REQ-13
@pytest.mark.tc("E2E-009")
def test_open_a_product(page: Page):
    """E2E-009"""
    # When the visitor opens the home page and clicks Asus TUF 15
    open_product(page, "Asus TUF 15")

    # Then the product page shows name, price, stock, a Quantity field and Add to Cart
    expect(page).to_have_url(url_ending("/product/1"))
    expect(page.get_by_role("heading", name="Asus TUF 15")).to_be_visible()
    expect(page.get_by_text("$9999.95")).to_be_visible()
    expect(page.get_by_text(re.compile(r"Stock: \d+"))).to_be_visible()
    expect(page.get_by_label("Quantity:")).to_be_visible()
    expect(page.get_by_role("button", name="Add to Cart")).to_be_visible()


@pytest.mark.tc("E2E-010")
def test_open_a_product_that_does_not_exist(page: Page):
    """E2E-010"""
    # When the visitor goes to the address /product/99999
    response = page.goto("/product/99999")

    # Then the page says "Product not found" (HTTP 404)
    assert response.status == 404
    expect(page.get_by_text("Product not found")).to_be_visible()


# ---------------------------------------------------------------- REQ-14
@pytest.mark.tc("E2E-011")
def test_add_a_product_to_the_cart(page: Page, set_stock):
    """E2E-011"""
    # Given Ear Plugs has at least 2 in stock (test helper)
    set_stock(EAR_PLUGS, 100)

    # When the visitor opens Ear Plugs, sets Quantity to 2 and clicks Add to Cart
    add_to_cart(page, "Ear Plugs", 2)

    # Then the cart opens and shows Ear Plugs: $9.95 x 2 = $19.90
    item = page.locator(".cart-item", has_text="Ear Plugs")
    expect(item).to_contain_text("$9.95 × 2 = $19.90")
    # And the menu shows Cart (1)
    expect(cart_link(page, 1)).to_be_visible()


# ---------------------------------------------------------------- REQ-15
@pytest.mark.tc("E2E-012")
def test_try_to_add_more_than_is_in_stock(page: Page, set_stock):
    """E2E-012"""
    # Given Lenovo Yoga 3 has exactly 10 in stock (test helper)
    set_stock(LENOVO, 10)

    # When the visitor opens Lenovo Yoga 3, sets Quantity to 11 and clicks Add to Cart
    open_product(page, "Lenovo Yoga 3")
    page.get_by_label("Quantity:").fill("11")
    page.get_by_role("button", name="Add to Cart").click()

    # Then nothing is added: still on the product page, menu still shows Cart (0)
    expect(page).to_have_url(url_ending("/product/2"))
    expect(cart_link(page, 0)).to_be_visible()
    page.goto("/cart")
    expect(page.get_by_text("Your cart is empty.")).to_be_visible()


# ---------------------------------------------------------------- REQ-16
@pytest.mark.tc("E2E-013")
def test_remove_a_product_from_the_cart(page: Page, start_logged_in):
    """E2E-013"""
    # Given the cart contains Ear Plugs and JBL 775 NC (test helper)
    start_logged_in(cart=[(EAR_PLUGS, 1), (JBL, 1)])

    # When the visitor opens Cart and clicks Remove next to Ear Plugs
    page.goto("/cart")
    page.locator(".cart-item", has_text="Ear Plugs").get_by_role("button", name="Remove").click()

    # Then Ear Plugs is no longer listed
    expect(page.locator(".cart-item", has_text="Ear Plugs")).to_have_count(0)
    expect(page.locator(".cart-item", has_text="JBL 775 NC")).to_be_visible()
    # And the subtotal is updated (only JBL 775 NC left)
    expect(page.get_by_text("Subtotal: $1200.00")).to_be_visible()
    # And the menu shows Cart (1)
    expect(cart_link(page, 1)).to_be_visible()


# ---------------------------------------------------------------- REQ-17
@pytest.mark.tc("E2E-014")
def test_cart_is_kept_when_you_log_in(page: Page, new_user, set_stock):
    """E2E-014"""
    # Given a customer account, not logged in (test helper)
    customer = new_user()
    set_stock(EAR_PLUGS, 100)

    # When they add Ear Plugs to the cart, log in and open Cart
    add_to_cart(page, "Ear Plugs", 3)
    log_in(page, customer["email"], customer["password"])
    page.goto("/cart")

    # Then Ear Plugs is still in the cart with the same quantity
    expect(page.locator(".cart-item", has_text="Ear Plugs")).to_contain_text("× 3")


# ---------------------------------------------------------------- REQ-43
@pytest.mark.tc("E2E-023")
def test_products_are_shown_as_cards(page: Page):
    """E2E-023"""
    # When the visitor opens the home page
    page.goto("/")

    # Then every product is a card with an image, the name, a price like $9999.95 and the stock
    cards = page.locator(".product-card")
    expect(cards.first).to_be_visible()
    for i in range(cards.count()):
        card = cards.nth(i)
        name = card.locator(".product-name").inner_text()
        assert name, f"card {i + 1} has no name"
        image = card.get_by_role("img")
        expect(image).to_have_attribute("alt", name)
        assert image_is_shown(image), f"the image of {name} is broken"
        expect(card.locator(".price")).to_have_text(re.compile(r"^\$\d+\.\d{2}$"))
        expect(card.locator(".stock")).to_have_text(re.compile(r"Stock: \d+"))

    # And clicking a card opens that product's page
    cards.filter(has_text="Ear Plugs").click()
    expect(page).to_have_url(url_ending(f"/product/{EAR_PLUGS}"))
    expect(page.get_by_role("heading", name="Ear Plugs")).to_be_visible()


# ---------------------------------------------------------------- REQ-45
@pytest.mark.tc("E2E-025")
def test_image_that_cannot_be_loaded(page: Page, set_image):
    """E2E-025"""
    # Given the image of Ear Plugs cannot be loaded (test helper: Ear Plugs gets
    # an image file name, and the browser's request for that file is blocked)
    set_image(EAR_PLUGS, "ear-plugs.jpg")
    page.route("**/static/products/ear-plugs.jpg", lambda route: route.abort())

    # When the visitor opens the home page
    page.goto("/")

    # Then the Ear Plugs card shows the placeholder, not a broken image
    card = page.locator(".product-card", has_text="Ear Plugs")
    image = card.get_by_role("img")
    expect(image).to_have_attribute("src", PLACEHOLDER_IMAGE)
    assert image_is_shown(image)
    # And the name, price and stock are still shown
    expect(card.locator(".product-name")).to_have_text("Ear Plugs")
    expect(card.locator(".price")).to_have_text("$9.95")
    expect(card.locator(".stock")).to_have_text(re.compile(r"Stock: \d+"))

    # When the visitor opens Ear Plugs
    card.click()

    # Then the product page shows the placeholder too, with the name, price and stock
    image = page.locator(".product-detail").get_by_role("img")
    expect(image).to_have_attribute("src", PLACEHOLDER_IMAGE)
    assert image_is_shown(image)
    expect(page.get_by_role("heading", name="Ear Plugs")).to_be_visible()
    expect(page.get_by_text("$9.95")).to_be_visible()
    expect(page.get_by_text(re.compile(r"Stock: \d+"))).to_be_visible()
