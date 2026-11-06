"""Product service: product catalogue and stock levels."""
import os

from flask import Flask, jsonify

from servicekit import (Database, error, is_positive_int, json_body, required_env,
                        require_internal_token, setup_app)

app = Flask(__name__)
log = setup_app(app, "product-service")

require_internal_token(app, required_env("INTERNAL_API_TOKEN"))
db = Database.from_env("ProductsDB")


def product_to_dict(row):
    # Money is sent as a string ("9999.95"), never as a float, so no precision
    # is lost between the DECIMAL column and the client.
    return {"id": row[0], "name": row[1], "price": f"{row[2]:.2f}", "stock": row[3]}


def parse_quantity():
    """Read a positive integer "quantity" from the JSON body.
    Returns (quantity, None) or (None, error_response)."""
    data = json_body()
    if data is None:
        return None, error("Request body must be a JSON object", 400)
    quantity = data.get("quantity")
    if not is_positive_int(quantity):
        return None, error("Quantity must be a positive integer", 400)
    return quantity, None


@app.route("/health", methods=["GET"])
def health():
    """Readiness: the service can reach its database."""
    try:
        db.is_healthy()
        return jsonify({"status": "healthy"}), 200
    except Exception:
        log.exception("Health check failed")
        return jsonify({"status": "unhealthy"}), 500


@app.route("/products", methods=["GET"])
def list_products():
    try:
        with db.cursor() as cur:
            cur.execute("SELECT Id, Name, Price, Stock FROM Products ORDER BY Id")
            rows = cur.fetchall()
    except Exception:
        log.exception("Failed to list products")
        return error("Internal server error", 500)
    return jsonify([product_to_dict(r) for r in rows]), 200


@app.route("/products/<int:product_id>", methods=["GET"])
def get_product(product_id):
    try:
        with db.cursor() as cur:
            cur.execute("SELECT Id, Name, Price, Stock FROM Products WHERE Id = ?", product_id)
            row = cur.fetchone()
    except Exception:
        log.exception("Failed to fetch product %s", product_id)
        return error("Internal server error", 500)

    if row is None:
        return error("Product not found", 404)
    return jsonify(product_to_dict(row)), 200


@app.route("/products/<int:product_id>/decrease-stock", methods=["POST"])
def decrease_stock(product_id):
    """Body: {"quantity": <positive int>}

    The check and the update happen in ONE statement, so two requests at the
    same time cannot both pass the check and sell more than is in stock.
    """
    quantity, err = parse_quantity()
    if err:
        return err

    try:
        with db.cursor() as cur:
            cur.execute(
                "UPDATE Products SET Stock = Stock - ? OUTPUT INSERTED.Stock "
                "WHERE Id = ? AND Stock >= ?",
                (quantity, product_id, quantity),
            )
            row = cur.fetchone()
            if row is not None:
                log.info("Product %s stock -%s -> %s", product_id, quantity, row[0])
                return jsonify({"message": "Stock updated", "new_stock": row[0]}), 200

            # Nothing updated: either the product does not exist or stock is too low.
            cur.execute("SELECT Stock FROM Products WHERE Id = ?", product_id)
            existing = cur.fetchone()
    except Exception:
        log.exception("Failed to decrease stock for product %s", product_id)
        return error("Internal server error", 500)

    if existing is None:
        return error("Product not found", 404)
    return error(f"Insufficient stock. Available: {existing[0]}", 400)


@app.route("/products/<int:product_id>/increase-stock", methods=["POST"])
def increase_stock(product_id):
    """Body: {"quantity": <positive int>}

    Compensating action used by order-service to give stock back when an order
    could not be completed after the stock had already been decreased.
    """
    quantity, err = parse_quantity()
    if err:
        return err

    try:
        with db.cursor() as cur:
            cur.execute(
                "UPDATE Products SET Stock = Stock + ? OUTPUT INSERTED.Stock WHERE Id = ?",
                (quantity, product_id),
            )
            row = cur.fetchone()
    except Exception:
        log.exception("Failed to increase stock for product %s", product_id)
        return error("Internal server error", 500)

    if row is None:
        return error("Product not found", 404)
    log.info("Product %s stock +%s -> %s", product_id, quantity, row[0])
    return jsonify({"message": "Stock updated", "new_stock": row[0]}), 200


if __name__ == "__main__":
    # Local development only. In the container the app runs under gunicorn.
    app.run(host="0.0.0.0", port=5000, debug=os.getenv("FLASK_DEBUG") == "1")
