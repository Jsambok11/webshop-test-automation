"""Frontend: server-rendered web shop (Flask + Jinja) on top of the backend services."""
import hmac
import os
import secrets
from decimal import Decimal, InvalidOperation

import requests
from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFError, CSRFProtect

from servicekit import ServiceClient, error_message, required_env, setup_app

app = Flask(__name__)
log = setup_app(app, "frontend-service")


def env_flag(name, default):
    return os.getenv(name, default).lower() in ("1", "true", "yes")


# ------------------- CONFIGURATION -------------------
secret_key = os.getenv("SECRET_KEY")
if not secret_key:
    # Works for a single process only: sessions are lost on restart and not
    # shared between pods. The Kubernetes deployment sets SECRET_KEY from a Secret.
    log.warning("SECRET_KEY is not set; using a random key for this process")
    secret_key = secrets.token_hex(32)

app.config.update(
    SECRET_KEY=secret_key,
    SESSION_COOKIE_HTTPONLY=True,     # JavaScript cannot read the session cookie
    SESSION_COOKIE_SAMESITE="Lax",    # not sent on cross-site POSTs
    # Only send the cookie over HTTPS. Enable when the site is served over TLS.
    SESSION_COOKIE_SECURE=env_flag("SESSION_COOKIE_SECURE", "false"),
    # CSRF tokens on every form. Can be switched off for legacy scripts that
    # post forms without a token (CSRF_ENABLED=false); never in production.
    WTF_CSRF_ENABLED=env_flag("CSRF_ENABLED", "true"),
    WTF_CSRF_TIME_LIMIT=None,         # token valid for the whole session
    RATELIMIT_ENABLED=env_flag("RATE_LIMIT_ENABLED", "true"),
    RATELIMIT_HEADERS_ENABLED=True,
)

csrf = CSRFProtect(app)
# In-memory counters are per pod. Behind a load balancer, use a shared store
# (storage_uri="redis://...") so the limit applies across all replicas.
limiter = Limiter(get_remote_address, app=app, storage_uri="memory://")
AUTH_RATE_LIMIT = os.getenv("AUTH_RATE_LIMIT", "5 per minute")

client = ServiceClient(required_env("INTERNAL_API_TOKEN"))
PRODUCT_SERVICE_URL = os.getenv("PRODUCT_SERVICE_URL", "http://product-service:80")
USER_SERVICE_URL = os.getenv("USER_SERVICE_URL", "http://user-service:80")
ORDER_SERVICE_URL = os.getenv("ORDER_SERVICE_URL", "http://order-service:80")

MAX_QUANTITY_PER_ITEM = 1000
DELIVERY_FEES = {"door": Decimal("5.00"), "pickup": Decimal("0.00")}
# Door delivery is free from this items total (inclusive). The fee itself does not count.
FREE_DOOR_DELIVERY_FROM = Decimal("100.00")
DELIVERY_LABELS = {"door": "Delivery to door", "pickup": "Pickup at store"}
SERVICE_UNAVAILABLE = "The service is temporarily unavailable. Please try again later."


# ------------------- SECURITY HEADERS -------------------
@app.after_request
def security_headers(response):
    response.headers.setdefault(
        "Content-Security-Policy",
        # Inline styles and the inline confirm() handler in base.html need
        # 'unsafe-inline'; everything else may only come from this site.
        "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'self'",
    )
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


# ------------------- ERROR HANDLERS -------------------
def error_page(message, status):
    return render_template("error.html", message=message), status


@app.errorhandler(CSRFError)
def csrf_error(_exc):
    return error_page("Your form has expired or is invalid. Please go back, reload the page and try again.", 400)


@app.errorhandler(429)
def rate_limited(_exc):
    template = "register.html" if request.path == url_for("register") else "login.html"
    return render_template(template, error="Too many attempts. Please wait a minute and try again."), 429


# ------------------- MONEY -------------------
def to_money(value):
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return Decimal("0.00")


@app.template_filter("money")
def money_filter(value):
    return f"{to_money(value):.2f}"


@app.template_global()
def image_url(product):
    """Address of a product's image (static/products/<file>), or of the
    placeholder when the product has none (null or empty)."""
    if product.get("image"):
        return url_for("static", filename=f"products/{product['image']}")
    return url_for("static", filename="placeholder.svg")


def delivery_fee(method, items_total):
    """Fee for a delivery method; door delivery is free from FREE_DOOR_DELIVERY_FROM."""
    if method == "door" and to_money(items_total) >= FREE_DOOR_DELIVERY_FROM:
        return Decimal("0.00")
    return DELIVERY_FEES[method]


# ------------------- HELPERS -------------------
def get_cart():
    if "cart" not in session:
        session["cart"] = []
    return session["cart"]


def fetch_product(product_id):
    """Return (product, status). status is 200, 404 or None when unreachable."""
    try:
        resp = client.get(f"{PRODUCT_SERVICE_URL}/products/{product_id}")
    except requests.RequestException:
        log.exception("Product service unreachable")
        return None, None
    if resp.status_code == 200:
        return resp.json(), 200
    if resp.status_code == 404:
        return None, 404
    log.error("Product service returned HTTP %s for product %s", resp.status_code, product_id)
    return None, None


def log_in(user):
    """Store the user in the session, keeping a cart built before logging in."""
    cart = session.get("cart", [])
    session.clear()
    session["cart"] = cart
    session["user_id"] = user["id"]
    session["user_email"] = user["email"]


def checkout_token():
    """One token per checkout attempt. It is sent to order-service as the
    Idempotency-Key, so submitting the same checkout twice creates one order."""
    if "checkout_token" not in session:
        session["checkout_token"] = secrets.token_hex(16)
    return session["checkout_token"]


# ------------------- PROBES -------------------
@app.route("/health")
def health():
    return jsonify({"status": "healthy"}), 200


# ------------------- PRODUCT PAGES -------------------
@app.route("/")
def index():
    products = []
    try:
        resp = client.get(f"{PRODUCT_SERVICE_URL}/products")
        if resp.status_code == 200:
            products = resp.json()
        else:
            log.error("Product list returned HTTP %s", resp.status_code)
            flash("Products could not be loaded right now.", "error")
    except requests.RequestException:
        log.exception("Product service unreachable")
        flash("Products could not be loaded right now.", "error")
    return render_template("index.html", products=products)


@app.route("/product/<int:product_id>")
def product_detail(product_id):
    product, status = fetch_product(product_id)
    if status == 404:
        return error_page("Product not found", 404)
    if product is None:
        return error_page(SERVICE_UNAVAILABLE, 503)
    return render_template("product.html", product=product)


# ------------------- CART -------------------
@app.route("/add-to-cart/<int:product_id>", methods=["POST"])
def add_to_cart(product_id):
    try:
        quantity = int(request.form.get("quantity", "1"))
    except ValueError:
        quantity = 0
    if not 1 <= quantity <= MAX_QUANTITY_PER_ITEM:
        flash(f"Quantity must be a whole number between 1 and {MAX_QUANTITY_PER_ITEM}.", "error")
        return redirect(url_for("product_detail", product_id=product_id))

    product, status = fetch_product(product_id)
    if status == 404:
        return error_page("Product not found", 404)
    if product is None:
        flash(SERVICE_UNAVAILABLE, "error")
        return redirect(url_for("product_detail", product_id=product_id))

    cart = get_cart()
    existing = next((item for item in cart if item["product_id"] == product_id), None)
    new_total = quantity + (existing["quantity"] if existing else 0)
    if new_total > product["stock"]:
        flash(f"Only {product['stock']} of {product['name']} in stock.", "error")
        return redirect(url_for("product_detail", product_id=product_id))

    if existing:
        existing["quantity"] = new_total
    else:
        cart.append({"product_id": product_id, "quantity": quantity})
    session.modified = True
    return redirect(url_for("cart"))


@app.route("/cart")
def cart():
    cart_items = []
    total = Decimal("0.00")
    kept = []
    for item in get_cart():
        product, status = fetch_product(item["product_id"])
        if status == 404:
            flash(f"Product {item['product_id']} is no longer available and was removed from your cart.", "error")
            continue
        kept.append(item)
        if product is None:
            flash("Some cart items could not be loaded right now.", "error")
            continue
        subtotal = to_money(product["price"]) * item["quantity"]
        total += subtotal
        cart_items.append({**product, "quantity": item["quantity"], "subtotal": subtotal})

    if len(kept) != len(get_cart()):
        session["cart"] = kept
    return render_template("cart.html", items=cart_items, total=total,
                           door_fee=delivery_fee("door", total),
                           checkout_token=checkout_token() if cart_items else None)


@app.route("/remove-from-cart/<int:product_id>", methods=["POST"])
def remove_from_cart(product_id):
    session["cart"] = [item for item in get_cart() if item["product_id"] != product_id]
    return redirect(url_for("cart"))


# ------------------- AUTH -------------------
@app.route("/login", methods=["GET", "POST"])
@limiter.limit(AUTH_RATE_LIMIT, methods=["POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        try:
            resp = client.post(f"{USER_SERVICE_URL}/login", json={"email": email, "password": password})
        except requests.RequestException:
            log.exception("User service unreachable")
            return render_template("login.html", error=SERVICE_UNAVAILABLE), 503

        if resp.status_code == 200:
            log_in(resp.json())
            return redirect(url_for("index"))
        if resp.status_code in (400, 401):
            return render_template("login.html", error=error_message(resp, "Invalid email or password")), resp.status_code
        log.error("Login returned HTTP %s", resp.status_code)
        return render_template("login.html", error=SERVICE_UNAVAILABLE), 503

    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
@limiter.limit(AUTH_RATE_LIMIT, methods=["POST"])
def register():
    if request.method == "POST":
        form = {
            "firstname": request.form.get("firstname", "").strip(),
            "surname": request.form.get("surname", "").strip(),
            "email": request.form.get("email", "").strip(),
        }
        password = request.form.get("password", "")

        def show_error(message, status):
            return render_template("register.html", error=message, **form), status

        try:
            resp = client.post(f"{USER_SERVICE_URL}/users", json={**form, "password": password})
            if resp.status_code in (400, 409):
                return show_error(error_message(resp, "Registration failed"), resp.status_code)
            if resp.status_code != 201:
                log.error("Register returned HTTP %s", resp.status_code)
                return show_error(SERVICE_UNAVAILABLE, 503)

            login_resp = client.post(f"{USER_SERVICE_URL}/login",
                                     json={"email": form["email"], "password": password})
        except requests.RequestException:
            log.exception("User service unreachable")
            return show_error(SERVICE_UNAVAILABLE, 503)

        if login_resp.status_code == 200:
            log_in(login_resp.json())
            return redirect(url_for("index"))
        return redirect(url_for("login"))

    return render_template("register.html")


# POST so another site cannot log users out with a link or an <img> tag.
@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/delete-account", methods=["POST"])
def delete_account():
    if "user_id" not in session:
        flash("You must be logged in to delete your account.", "error")
        return redirect(url_for("login"))

    try:
        resp = client.delete(f"{USER_SERVICE_URL}/users/{session['user_id']}")
    except requests.RequestException:
        log.exception("User service unreachable")
        flash(SERVICE_UNAVAILABLE, "error")
        return redirect(url_for("index"))

    if resp.status_code in (200, 404):
        # 404: the account is already gone, so the result is the same for the user.
        session.clear()
        flash("Your account has been successfully deleted.", "success")
    else:
        log.error("Delete account returned HTTP %s", resp.status_code)
        flash(SERVICE_UNAVAILABLE, "error")
    return redirect(url_for("index"))


# ------------------- CHECKOUT -------------------
@app.route("/checkout", methods=["POST"])
def checkout():
    """Place ONE order for the whole cart. Either everything is ordered or
    nothing is, and submitting the same checkout twice creates one order."""
    if "user_id" not in session:
        flash("Please log in to place your order.", "error")
        return redirect(url_for("login"))

    cart_items = get_cart()
    if not cart_items:
        return redirect(url_for("cart"))

    token = request.form.get("checkout_token", "")
    if not token or not hmac.compare_digest(token, session.get("checkout_token", "")):
        flash("Your cart has changed. Please review it and place the order again.", "error")
        return redirect(url_for("cart"))

    post_code = request.form.get("post_code", "").strip()
    delivery_method = request.form.get("delivery_method", "door")
    if not post_code or len(post_code) > 10:
        flash("Please enter a valid post code.", "error")
        return redirect(url_for("cart"))
    if delivery_method not in DELIVERY_FEES:
        flash("Please choose a delivery method.", "error")
        return redirect(url_for("cart"))

    try:
        resp = client.post(
            f"{ORDER_SERVICE_URL}/orders",
            json={
                "userId": session["user_id"],
                "items": [{"productId": i["product_id"], "quantity": i["quantity"]} for i in cart_items],
            },
            headers={"Idempotency-Key": token},
            # Creating an order calls two other services; allow more time.
            timeout=15,
        )
    except requests.RequestException:
        log.exception("Order service unreachable")
        flash(SERVICE_UNAVAILABLE, "error")
        return redirect(url_for("cart"))

    if resp.status_code not in (200, 201):  # 200 = replay of an order already placed
        message = error_message(resp, "Your order could not be placed.") if resp.status_code < 500 else SERVICE_UNAVAILABLE
        flash(f"Order not placed, nothing was charged: {message}", "error")
        return redirect(url_for("cart"))

    order = resp.json()
    session.pop("cart", None)
    session.pop("checkout_token", None)

    items_total = to_money(order["total"])
    fee = delivery_fee(delivery_method, items_total)
    receipt = {
        "order": order,
        "items_total": items_total,
        "delivery_fee": fee,
        "delivery_method": DELIVERY_LABELS[delivery_method],
        "post_code": post_code,
        "total": items_total + fee,
        # Mock payment: always approved.
        "payment": {"method": "Klarna", "transaction_id": f"KLARNA-{token[:12].upper()}"},
        "customer_email": session.get("user_email"),
    }
    log.info("[MOCK EMAIL] Receipt for order %s sent", order["id"])
    return render_template("receipt.html", receipt=receipt)


if __name__ == "__main__":
    # Local development only. In the container the app runs under gunicorn.
    app.run(host="0.0.0.0", port=5000, debug=os.getenv("FLASK_DEBUG") == "1")
