"""E2E tests for accounts: register, log in, log out, delete account."""
import uuid

import pytest
from playwright.sync_api import Page, expect

from shop_ui import log_in, register, url_ending, welcome


def new_email():
    return f"e2e-{uuid.uuid4().hex[:10]}@example.com"


# ---------------------------------------------------------------- REQ-01
@pytest.mark.tc("E2E-001")
def test_register_a_new_account(page: Page, delete_afterwards):
    """E2E-001"""
    # Given a visitor who is not logged in, with an email nobody uses yet
    email = new_email()
    delete_afterwards(email, "Secret123")

    # When they click Register, fill in the form with password Secret123
    # and click Create Account
    register(page, email, "Secret123")

    # Then the home page opens
    expect(page).to_have_url(url_ending("/"))
    # And the menu shows "Welcome, <their email>"
    expect(page.get_by_text(f"Welcome, {email}")).to_be_visible()


# ---------------------------------------------------------------- REQ-02
@pytest.mark.tc("E2E-002")
def test_register_with_a_too_short_password(page: Page, delete_afterwards):
    """E2E-002"""
    # Given a visitor who is not logged in
    email = new_email()
    delete_afterwards(email, "1234")   # in case the shop wrongly accepts it

    # When they register with password 1234
    register(page, email, "1234", firstname="Short", surname="Password")

    # Then the message explains the rule
    expect(page.get_by_text("Password must be 5-50 characters")).to_be_visible()
    # And name and email are still filled in
    expect(page.get_by_placeholder("First Name")).to_have_value("Short")
    expect(page.get_by_placeholder("Surname")).to_have_value("Password")
    expect(page.get_by_placeholder("Email")).to_have_value(email)
    # And they are not logged in
    expect(welcome(page)).to_have_count(0)


# ---------------------------------------------------------------- REQ-05
@pytest.mark.tc("E2E-003")
def test_register_with_an_email_that_is_taken(page: Page, new_user):
    """E2E-003"""
    # Given an account with email X exists (test helper)
    existing = new_user()

    # When a visitor registers with email X
    register(page, existing["email"], "Secret123")

    # Then they are told the email is taken, and are not logged in
    expect(page.get_by_text("A user with that email already exists")).to_be_visible()
    expect(welcome(page)).to_have_count(0)


# ---------------------------------------------------------------- REQ-06
@pytest.mark.tc("E2E-004")
def test_log_in(page: Page, new_user):
    """E2E-004"""
    # Given a customer account (test helper)
    customer = new_user()

    # When the customer clicks Login, enters email and password and clicks Log In
    log_in(page, customer["email"], customer["password"])

    # Then the home page opens
    expect(page).to_have_url(url_ending("/"))
    # And the menu shows "Welcome, <email>", Logout and Delete Account
    expect(page.get_by_text(f"Welcome, {customer['email']}")).to_be_visible()
    expect(page.get_by_role("button", name="Logout")).to_be_visible()
    expect(page.get_by_role("button", name="Delete Account")).to_be_visible()


@pytest.mark.tc("E2E-005")
def test_log_out(page: Page, new_user, start_logged_in):
    """E2E-005"""
    # Given a logged-in customer (test helper)
    start_logged_in(new_user())
    page.goto("/")
    expect(welcome(page)).to_be_visible()

    # When they click Logout
    page.get_by_role("button", name="Logout").click()

    # Then the home page opens
    expect(page).to_have_url(url_ending("/"))
    # And the menu shows Login and Register again, and no "Welcome"
    expect(page.get_by_role("link", name="Login")).to_be_visible()
    expect(page.get_by_role("link", name="Register")).to_be_visible()
    expect(welcome(page)).to_have_count(0)


# ---------------------------------------------------------------- REQ-07
@pytest.mark.tc("E2E-006")
def test_log_in_with_a_wrong_password(page: Page, new_user):
    """E2E-006"""
    # Given a customer account (test helper)
    customer = new_user()

    # When someone logs in with the right email and a wrong password
    log_in(page, customer["email"], "wrong-password")

    # Then the message says the combination is wrong
    expect(page.get_by_text("Invalid email or password")).to_be_visible()
    # And they stay on the login page, not logged in
    expect(page).to_have_url(url_ending("/login"))
    expect(welcome(page)).to_have_count(0)


# ---------------------------------------------------------------- REQ-09
@pytest.mark.tc("E2E-007")
def test_delete_your_account(page: Page, new_user, start_logged_in):
    """E2E-007"""
    # Given a logged-in customer (test helper)
    customer = new_user()
    start_logged_in(customer)
    page.goto("/")

    # When they click Delete Account and confirm with OK
    page.once("dialog", lambda dialog: dialog.accept())
    page.get_by_role("button", name="Delete Account").click()

    # Then the deletion is confirmed and they are logged out
    expect(page.get_by_text("Your account has been successfully deleted.")).to_be_visible()
    expect(welcome(page)).to_have_count(0)

    # When they try to log in again with the same email and password
    log_in(page, customer["email"], customer["password"])

    # Then the login fails
    expect(page.get_by_text("Invalid email or password")).to_be_visible()
