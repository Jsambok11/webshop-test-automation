"""E2E security test: user input is shown as text, never executed."""
import uuid

import pytest
from playwright.sync_api import Page, expect


# ---------------------------------------------------------------- REQ-33
@pytest.mark.tc("E2E-021")
def test_script_in_user_data_is_shown_as_text(page: Page, new_user, start_logged_in):
    """E2E-021

    The account is logged in by the test helper: the browser's own email check
    on the login form refuses an address containing <script>, so the form
    cannot be used for it. What this test checks is how the page shows the
    stored email afterwards."""
    # Given an account whose email contains a script (test helper)
    email = f"<script>alert(1)</script>{uuid.uuid4().hex[:6]}@x.io"
    start_logged_in(new_user(email=email))
    # Record every pop-up the page might open
    dialogs = []
    page.on("dialog", lambda dialog: (dialogs.append(dialog.message), dialog.dismiss()))

    # When the customer opens the shop logged in as that account
    page.goto("/")

    # Then the menu shows the email as plain text
    expect(page.get_by_text(f"Welcome, {email}")).to_be_visible()
    # And no pop-up appeared, i.e. the script was not executed
    assert dialogs == []
