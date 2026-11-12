"""Page actions used by the E2E tests, named like the steps in the test cases.

Locators use what a user sees (roles, labels, placeholders, visible text), so
the tests keep working when the HTML structure changes.
"""
import re
import time

from playwright.sync_api import Page, expect

# Login and registration allow 5 attempts per minute per IP (REQ-32). If an
# earlier run used them up, wait for the next window once instead of failing.
RATE_LIMIT_WINDOW = 65


def _submit_form(page: Page, path: str, button: str, fill):
    """Open the form, fill it in, submit it, and return the server's answer."""
    for attempt in (1, 2):
        page.goto(path)
        fill()
        with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith(path)) as answer:
            page.get_by_role("button", name=button).click()
        if answer.value.status != 429:
            return answer.value
        if attempt == 1:
            time.sleep(RATE_LIMIT_WINDOW)
    raise AssertionError(f"{path} is still rate limited after waiting {RATE_LIMIT_WINDOW} s")


def log_in(page: Page, email: str, password: str):
    """Click Login, enter email and password, click Log In."""
    def fill():
        page.get_by_placeholder("Email").fill(email)
        page.get_by_placeholder("Password").fill(password)
    return _submit_form(page, "/login", "Log In", fill)


def register(page: Page, email: str, password: str, firstname="E2E", surname="Tester"):
    """Click Register, fill in the form, click Create Account."""
    def fill():
        page.get_by_placeholder("First Name").fill(firstname)
        page.get_by_placeholder("Surname").fill(surname)
        page.get_by_placeholder("Email").fill(email)
        page.get_by_placeholder("Password").fill(password)
    return _submit_form(page, "/register", "Create Account", fill)


def open_product(page: Page, name: str):
    """Open the home page and click a product."""
    page.goto("/")
    page.get_by_text(name, exact=True).click()


def add_to_cart(page: Page, name: str, quantity: int = 1):
    """Open a product, set Quantity, click Add to Cart."""
    open_product(page, name)
    page.get_by_label("Quantity:").fill(str(quantity))
    page.get_by_role("button", name="Add to Cart").click()
    expect(page).to_have_url(url_ending("/cart"))


def start_checkout(page: Page, post_code: str = "12345", delivery: str = "door"):
    """Open Cart, enter the post code and choose the delivery method."""
    page.goto("/cart")
    page.get_by_placeholder("e.g., 12345").fill(post_code)
    label = "Pickup at store" if delivery == "pickup" else "Delivery to door"
    page.get_by_label(label).check()


def check_out(page: Page, post_code: str = "12345", delivery: str = "door"):
    """Open Cart, enter the post code, choose delivery, click Place Order."""
    start_checkout(page, post_code, delivery)
    page.get_by_role("button", name="Place Order").click()


PLACEHOLDER_IMAGE = re.compile(r"/static/placeholder\.svg$")


def image_is_shown(img) -> bool:
    """True when the browser loaded and decoded the image, False for a broken image."""
    return img.evaluate("i => i.decode().then(() => i.naturalWidth > 0, () => false)")


def url_ending(path: str):
    """Match a page URL by its path, e.g. url_ending("/cart")."""
    return re.compile(re.escape(path) + r"$")


def cart_link(page: Page, count: int):
    """The 'Cart (n)' link in the menu."""
    return page.get_by_role("link", name=f"Cart ({count})")


def welcome(page: Page):
    """The 'Welcome, <email>' text shown in the menu when logged in."""
    return page.get_by_text("Welcome,")
