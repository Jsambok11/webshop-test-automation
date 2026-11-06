"""Integration tests for accounts: user-service with its real database, and
the order-service it calls when an account is deleted."""
import concurrent.futures
import uuid

import pytest

SEED_USERS = [("alice.anderson@example.com", "Pass12345"),
              ("brian.bergstrom@example.com", "SecurePass9"),
              ("clara.carlsson@example.com", "MyPassword123")]


def unique_email(prefix="it"):
    return f"{prefix}-{uuid.uuid4().hex[:12]}@example.com"


# ---------------------------------------------------------------- REQ-01
@pytest.mark.tc("IT-001")
def test_create_user_returns_the_user_without_password(api, urls):
    """IT-001"""
    # Given a new, unique email
    email = unique_email("IT001")    # upper case on purpose: stored lower case
    body = {"firstname": "Ada", "surname": "Lovelace", "email": email, "password": "Secret123"}

    # When the user is created
    resp = api.post(f"{urls['user']}/users", json=body)
    try:
        # Then the user is returned, with the email lower-cased and no password
        assert resp.status_code == 201
        user = resp.json()
        assert set(user) == {"id", "firstname", "surname", "email"}
        assert user["email"] == email.lower()
        assert "Secret123" not in resp.text
    finally:
        if resp.status_code == 201:
            api.delete(f"{urls['user']}/users/{resp.json()['id']}")


# ---------------------------------------------------------------- REQ-05
@pytest.mark.tc("IT-002")
def test_duplicate_email_is_rejected(api, urls, new_user):
    """IT-002"""
    # Given a registered user
    user = new_user()

    # When someone registers with the same email
    resp = api.post(f"{urls['user']}/users", json={"firstname": "B", "surname": "C",
                                                   "email": user["email"], "password": "Secret123"})

    # Then it is rejected as a conflict
    assert resp.status_code == 409
    assert resp.json() == {"error": "A user with that email already exists"}


@pytest.mark.tc("IT-003")
def test_email_uniqueness_ignores_letter_case(api, urls, new_user):
    """IT-003"""
    # Given a user registered with a lower-case email
    user = new_user(email=unique_email("casetest"))

    # When the same email is registered in upper case
    resp = api.post(f"{urls['user']}/users", json={"firstname": "B", "surname": "C",
                                                   "email": user["email"].upper(), "password": "Secret123"})

    # Then it counts as the same email
    assert user["email"] == user["email"].lower()
    assert resp.status_code == 409


@pytest.mark.tc("IT-004")
def test_simultaneous_registrations_with_the_same_email(api, urls, sql):
    """IT-004: two requests at the same moment. Whichever comes second must get
    409, either from the service's check or from the UNIQUE constraint."""
    email = unique_email()
    body = {"firstname": "Race", "surname": "Test", "email": email, "password": "Secret123"}

    # When two registrations are sent in parallel
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        replies = list(pool.map(lambda _: api.post(f"{urls['user']}/users", json=body), range(2)))
    try:
        # Then exactly one succeeds
        assert sorted(r.status_code for r in replies) == [201, 409]
        # And the database has exactly one user with that email
        rc, out = sql.run(f"SELECT COUNT(*) FROM UsersDB.dbo.Users WHERE Email = N'{email}'")
        assert rc == 0, out
        assert out.strip() == "1"
    finally:
        for r in replies:
            if r.status_code == 201:
                api.delete(f"{urls['user']}/users/{r.json()['id']}")


# ---------------------------------------------------------------- REQ-06
@pytest.mark.tc("IT-005")
def test_login_with_correct_credentials_and_any_letter_case(api, urls, new_user):
    """IT-005"""
    user = new_user()

    for email in (user["email"], user["email"].upper()):
        # When logging in (also with the email in upper case)
        resp = api.post(f"{urls['user']}/login", json={"email": email, "password": user["password"]})

        # Then the user is returned, without password or hash
        assert resp.status_code == 200
        assert resp.json() == {"id": user["id"], "firstname": "Test", "surname": "User", "email": user["email"]}
        assert "scrypt" not in resp.text


@pytest.mark.tc("IT-006")
@pytest.mark.parametrize("email, password", SEED_USERS, ids=["alice", "brian", "clara"])
def test_seed_users_can_log_in(api, urls, email, password):
    """IT-006: proves the seed data's password hashes are valid."""
    resp = api.post(f"{urls['user']}/login", json={"email": email, "password": password})

    assert resp.status_code == 200


# ---------------------------------------------------------------- REQ-07
@pytest.mark.tc("IT-007")
def test_wrong_password_and_unknown_email_give_the_same_error(api, urls, new_user):
    """IT-007: an attacker must not learn which emails are registered."""
    user = new_user()

    wrong_password = api.post(f"{urls['user']}/login", json={"email": user["email"], "password": "wrong-pass"})
    unknown_email = api.post(f"{urls['user']}/login", json={"email": unique_email(), "password": "wrong-pass"})

    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json() == unknown_email.json() == {"error": "Invalid email or password"}


# ---------------------------------------------------------------- REQ-08
@pytest.mark.tc("IT-008")
def test_stored_password_is_a_hash(new_user, sql):
    """IT-008: look directly in the database."""
    # Given a user registered with password Secret123
    user = new_user(password="Secret123")

    # When the stored value is read from the database
    rc, stored = sql.run(f"SELECT PasswordHash FROM UsersDB.dbo.Users WHERE Email = N'{user['email']}'")

    # Then it is a scrypt hash, not the password
    assert rc == 0, stored
    assert stored.startswith("scrypt:")
    assert "Secret123" not in stored


# ---------------------------------------------------------------- REQ-09
@pytest.mark.tc("IT-009")
def test_delete_user(api, urls, new_user):
    """IT-009"""
    user = new_user()

    # When the user is deleted
    deleted = api.delete(f"{urls['user']}/users/{user['id']}")
    # Then the user is gone, and deleting again finds nothing
    assert deleted.status_code == 200
    assert deleted.json() == {"message": "User deleted successfully"}
    assert api.get(f"{urls['user']}/users/{user['id']}").status_code == 404
    assert api.delete(f"{urls['user']}/users/{user['id']}").status_code == 404


# ---------------------------------------------------------------- REQ-10
@pytest.mark.tc("IT-010")
def test_orders_remain_after_account_deletion(api, urls, new_user, set_stock, place_order):
    """IT-010: user-service asks order-service to anonymize before deleting."""
    # Given a user with one order
    user = new_user()
    set_stock(5, 10)
    order = place_order(user["id"], (5, 1)).json()

    # When the user deletes their account
    assert api.delete(f"{urls['user']}/users/{user['id']}").status_code == 200

    # Then the order still exists, but is no longer linked to the user
    kept = api.get(f"{urls['order']}/orders/{order['id']}").json()
    assert kept["userId"] is None
    assert kept["items"] == order["items"]
