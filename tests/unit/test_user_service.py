"""Unit tests for user-service: registration validation, login input, password
hashing and account deletion. Database and order-service are mocked."""
import pytest
import requests
from werkzeug.security import check_password_hash

from support import AUTH, response, sql_calls, user_body

NEW_USER_ID = 42


def accept_new_user(svc):
    """Fake DB answers for a successful registration:
    the email is not taken (None), then the INSERT returns the new id."""
    svc.cursor.fetchone.side_effect = [None, (NEW_USER_ID,)]


# ---------------------------------------------------------------- REQ-01
@pytest.mark.tc("UT-001")
@pytest.mark.parametrize("missing", ["firstname", "surname", "email", "password"])
def test_missing_registration_field_is_rejected(user_service, missing):
    """UT-001: every field is required."""
    # Given a registration request without one of the fields
    body = user_body()
    del body[missing]

    # When it is sent to POST /users
    resp = user_service.api.post("/users", json=body, headers=AUTH)

    # Then it is rejected before anything is written to the database
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "Missing required fields"}
    user_service.cursor.execute.assert_not_called()


@pytest.mark.tc("UT-002")
@pytest.mark.parametrize("kwargs", [
    {"data": "not json", "content_type": "text/plain"},   # not JSON at all
    {"json": [1]},                                         # JSON, but a list instead of an object
], ids=["text-body", "json-list"])
def test_registration_body_that_is_not_a_json_object_is_rejected(user_service, kwargs):
    """UT-002"""
    resp = user_service.api.post("/users", headers=AUTH, **kwargs)

    assert resp.status_code == 400
    assert resp.get_json() == {"error": "Request body must be a JSON object"}


# ---------------------------------------------------------------- REQ-02
@pytest.mark.tc("UT-003")
@pytest.mark.parametrize("length, expected_status", [
    (4, 400),    # one below the minimum
    (5, 201),    # minimum
    (50, 201),   # maximum
    (51, 400),   # one above the maximum
])
def test_password_length_boundaries(user_service, length, expected_status):
    """UT-003: boundary value analysis on the 5-50 character rule."""
    # Given the email is free
    accept_new_user(user_service)

    # When registering with a password of the given length
    resp = user_service.api.post("/users", json=user_body(password="p" * length), headers=AUTH)

    # Then only 5..50 characters are accepted
    assert resp.status_code == expected_status
    if expected_status == 400:
        assert resp.get_json() == {"error": "Password must be 5-50 characters"}


# ---------------------------------------------------------------- REQ-03
@pytest.mark.tc("UT-004")
@pytest.mark.parametrize("email, valid", [
    ("a@b.c", True),     # valid class: text@text
    ("abc", False),      # no @
    ("@b.c", False),     # nothing before @
    ("a@", False),       # nothing after @
    ("@", False),        # only @
])
def test_email_format_equivalence_classes(user_service, email, valid):
    """UT-004: one representative per equivalence class."""
    accept_new_user(user_service)

    resp = user_service.api.post("/users", json=user_body(email=email), headers=AUTH)

    if valid:
        assert resp.status_code == 201
    else:
        assert resp.status_code == 400
        assert resp.get_json() == {"error": "Email must contain @"}


@pytest.mark.tc("UT-005")
@pytest.mark.parametrize("length, expected_status", [(50, 201), (51, 400)])
def test_email_length_boundary(user_service, length, expected_status):
    """UT-005: the database column holds 50 characters."""
    accept_new_user(user_service)
    domain = "@example.io"
    email = "a" * (length - len(domain)) + domain   # exactly `length` characters

    resp = user_service.api.post("/users", json=user_body(email=email), headers=AUTH)

    assert resp.status_code == expected_status
    if expected_status == 400:
        assert resp.get_json() == {"error": "Email must be at most 50 characters"}


# ---------------------------------------------------------------- REQ-04
@pytest.mark.tc("UT-006")
@pytest.mark.parametrize("firstname, expected_status, expected_error", [
    ("a" * 50, 201, None),
    ("a" * 51, 400, "Names must be at most 50 characters"),
    (5, 400, "Missing required fields"),        # a number is not a name
    ("   ", 400, "Missing required fields"),    # only spaces counts as empty
], ids=["50-chars", "51-chars", "number", "only-spaces"])
def test_name_length_and_type(user_service, firstname, expected_status, expected_error):
    """UT-006"""
    accept_new_user(user_service)

    resp = user_service.api.post("/users", json=user_body(firstname=firstname), headers=AUTH)

    assert resp.status_code == expected_status
    if expected_error:
        assert resp.get_json() == {"error": expected_error}


# ---------------------------------------------------------------- REQ-06
@pytest.mark.tc("UT-007")
@pytest.mark.parametrize("kwargs, expected_error", [
    ({"json": {"email": "a@b.c"}}, "Email and password required"),
    ({"json": {"password": "Secret123"}}, "Email and password required"),
    ({"data": "not json", "content_type": "text/plain"}, "Request body must be a JSON object"),
], ids=["no-password", "no-email", "not-json"])
def test_login_with_missing_or_invalid_input(user_service, kwargs, expected_error):
    """UT-007"""
    resp = user_service.api.post("/login", headers=AUTH, **kwargs)

    assert resp.status_code == 400
    assert resp.get_json() == {"error": expected_error}
    # The database is not even asked
    user_service.cursor.execute.assert_not_called()


# ---------------------------------------------------------------- REQ-08
@pytest.mark.tc("UT-008")
def test_service_hashes_the_password_before_saving(user_service):
    """UT-008: the plain password never reaches the database or the response."""
    # Given a new user with password Secret123
    accept_new_user(user_service)

    # When the user registers
    resp = user_service.api.post("/users", json=user_body(password="Secret123"), headers=AUTH)

    # Then the value given to the INSERT is a scrypt hash of the password ...
    assert resp.status_code == 201
    insert = next(c for c in user_service.cursor.execute.call_args_list if c.args[0].startswith("INSERT"))
    stored = insert.args[1][3]                      # (firstname, surname, email, hash)
    assert stored.startswith("scrypt:")
    assert stored != "Secret123"
    assert check_password_hash(stored, "Secret123")
    # ... and the response does not contain the password at all
    assert "password" not in resp.get_json()
    assert "Secret123" not in resp.get_data(as_text=True)


# ---------------------------------------------------------------- REQ-11
@pytest.mark.tc("UT-010")
@pytest.mark.parametrize("order_service_answer", [
    requests.ConnectionError("order-service unreachable"),
    response(500, {"error": "Internal server error"}),
], ids=["unreachable", "returns-500"])
def test_deletion_refused_when_orders_cannot_be_anonymized(user_service, order_service_answer):
    """UT-010: no user may be deleted while their orders still point at them."""
    # Given the user exists
    user_service.cursor.fetchone.return_value = (1,)
    # And order-service cannot anonymize the orders
    if isinstance(order_service_answer, Exception):
        user_service.outgoing.post.side_effect = order_service_answer
    else:
        user_service.outgoing.post.return_value = order_service_answer

    # When the user is deleted
    resp = user_service.api.delete("/users/1", headers=AUTH)

    # Then the deletion is refused and no DELETE statement is executed
    assert resp.status_code == 503
    assert resp.get_json() == {"error": "A required service is unavailable, try again later"}
    assert not any(sql.startswith("DELETE") for sql in sql_calls(user_service.cursor))
