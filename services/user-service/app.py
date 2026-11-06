"""User service: registration, login, lookup and deletion of users."""
import os

import pyodbc
import requests
from flask import Flask, jsonify
from werkzeug.security import check_password_hash, generate_password_hash

from servicekit import (Database, ServiceClient, error, json_body, required_env,
                        require_internal_token, setup_app)

app = Flask(__name__)
log = setup_app(app, "user-service")

INTERNAL_TOKEN = required_env("INTERNAL_API_TOKEN")
require_internal_token(app, INTERNAL_TOKEN)
db = Database.from_env("UsersDB")
client = ServiceClient(INTERNAL_TOKEN)
ORDER_SERVICE_URL = os.getenv("ORDER_SERVICE_URL", "http://order-service:80")

# Limits that mirror the database schema (NVARCHAR(50)).
NAME_MAX = 50
EMAIL_MAX = 50
PASSWORD_MIN = 5
PASSWORD_MAX = 50


def clean_str(value):
    """A stripped string, or None if the value is not a non-empty string."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def user_to_dict(row):
    return {"id": row[0], "firstname": row[1], "surname": row[2], "email": row[3]}


@app.route("/health", methods=["GET"])
def health():
    """Readiness: the service can reach its database."""
    try:
        db.is_healthy()
        return jsonify({"status": "healthy"}), 200
    except Exception:
        log.exception("Health check failed")
        return jsonify({"status": "unhealthy"}), 500


@app.route("/users", methods=["POST"])
def create_user():
    """Body: {"firstname", "surname", "email", "password"}"""
    data = json_body()
    if data is None:
        return error("Request body must be a JSON object", 400)

    firstname = clean_str(data.get("firstname"))
    surname = clean_str(data.get("surname"))
    email = clean_str(data.get("email"))
    password = data.get("password")  # not stripped: spaces are allowed in passwords

    if not firstname or not surname or not email or not isinstance(password, str) or not password:
        return error("Missing required fields", 400)

    email = email.lower()
    if len(firstname) > NAME_MAX or len(surname) > NAME_MAX:
        return error(f"Names must be at most {NAME_MAX} characters", 400)
    if len(email) > EMAIL_MAX:
        return error(f"Email must be at most {EMAIL_MAX} characters", 400)
    local, at, domain = email.partition("@")
    if not at or not local or not domain:
        return error("Email must contain @", 400)
    if not PASSWORD_MIN <= len(password) <= PASSWORD_MAX:
        return error(f"Password must be {PASSWORD_MIN}-{PASSWORD_MAX} characters", 400)

    password_hash = generate_password_hash(password)

    try:
        with db.cursor() as cur:
            cur.execute("SELECT 1 FROM Users WHERE Email = ?", email)
            if cur.fetchone():
                return error("A user with that email already exists", 409)
            cur.execute(
                "INSERT INTO Users (Firstname, Surname, Email, PasswordHash) "
                "OUTPUT INSERTED.Id VALUES (?, ?, ?, ?)",
                (firstname, surname, email, password_hash),
            )
            user_id = cur.fetchone()[0]
    except pyodbc.IntegrityError:
        # Two simultaneous registrations with the same email: the UNIQUE
        # constraint catches the one the SELECT above missed.
        return error("A user with that email already exists", 409)
    except Exception:
        log.exception("Failed to create user")
        return error("Internal server error", 500)

    log.info("Created user %s", user_id)
    return jsonify({"id": user_id, "firstname": firstname, "surname": surname, "email": email}), 201


@app.route("/login", methods=["POST"])
def login():
    """Body: {"email", "password"}. Returns the user (without password) or 401."""
    data = json_body()
    if data is None:
        return error("Request body must be a JSON object", 400)

    email = clean_str(data.get("email"))
    password = data.get("password")
    if not email or not isinstance(password, str) or not password:
        return error("Email and password required", 400)

    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT Id, Firstname, Surname, Email, PasswordHash FROM Users WHERE Email = ?",
                email.lower(),
            )
            row = cur.fetchone()
    except Exception:
        log.exception("Login query failed")
        return error("Internal server error", 500)

    # Same message for unknown email and wrong password, so the API does not
    # reveal which emails are registered.
    if row is None or not check_password_hash(row[4], password):
        log.info("Failed login attempt")
        return error("Invalid email or password", 401)
    return jsonify(user_to_dict(row)), 200


@app.route("/users/<int:user_id>", methods=["GET"])
def get_user(user_id):
    try:
        with db.cursor() as cur:
            cur.execute("SELECT Id, Firstname, Surname, Email FROM Users WHERE Id = ?", user_id)
            row = cur.fetchone()
    except Exception:
        log.exception("Failed to fetch user %s", user_id)
        return error("Internal server error", 500)

    if row is None:
        return error("User not found", 404)
    return jsonify(user_to_dict(row)), 200


@app.route("/users/<int:user_id>", methods=["DELETE"])
def delete_user(user_id):
    """Delete a user. Their orders are kept (bookkeeping) but anonymized first.

    The orders live in another service's database, so a foreign key with
    ON DELETE cannot be used. If order-service cannot anonymize the orders, the
    user is NOT deleted, so we never end up with orders pointing at a user that
    no longer exists.
    """
    try:
        with db.cursor() as cur:
            cur.execute("SELECT 1 FROM Users WHERE Id = ?", user_id)
            exists = cur.fetchone() is not None
    except Exception:
        log.exception("Failed to look up user %s", user_id)
        return error("Internal server error", 500)
    if not exists:
        return error("User not found", 404)

    try:
        resp = client.post(f"{ORDER_SERVICE_URL}/users/{user_id}/orders/anonymize")
    except requests.RequestException:
        log.exception("Order service unreachable while deleting user %s", user_id)
        return error("A required service is unavailable, try again later", 503)
    if resp.status_code != 200:
        log.error("Anonymizing orders for user %s returned HTTP %s", user_id, resp.status_code)
        return error("A required service is unavailable, try again later", 503)

    try:
        with db.cursor() as cur:
            cur.execute("DELETE FROM Users WHERE Id = ?", user_id)
            affected = cur.rowcount
    except Exception:
        log.exception("Failed to delete user %s", user_id)
        return error("Internal server error", 500)

    if affected == 0:
        return error("User not found", 404)
    log.info("Deleted user %s", user_id)
    return jsonify({"message": "User deleted successfully"}), 200


if __name__ == "__main__":
    # Local development only. In the container the app runs under gunicorn.
    app.run(host="0.0.0.0", port=5000, debug=os.getenv("FLASK_DEBUG") == "1")
