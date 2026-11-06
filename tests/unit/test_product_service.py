"""Unit tests for product-service: stock change validation. Database mocked."""
import pytest

from support import AUTH

MISSING = object()   # marker: leave "quantity" out of the body


# ---------------------------------------------------------------- REQ-23
@pytest.mark.tc("UT-026")
@pytest.mark.parametrize("endpoint", ["decrease-stock", "increase-stock"])
@pytest.mark.parametrize("quantity", [0, -1, "1", True, 1.5, MISSING],
                         ids=["zero", "negative", "string", "bool", "float", "missing"])
def test_invalid_stock_quantity_is_rejected(product_service, endpoint, quantity):
    """UT-026: only a positive whole number is a valid quantity.
    (True counts as 1 in Python, so it needs an explicit check.)"""
    body = {} if quantity is MISSING else {"quantity": quantity}

    resp = product_service.api.post(f"/products/1/{endpoint}", json=body, headers=AUTH)

    assert resp.status_code == 400
    assert resp.get_json() == {"error": "Quantity must be a positive integer"}
    # Rejected before any SQL is run
    product_service.db.cursor.assert_not_called()


@pytest.mark.tc("UT-026")
@pytest.mark.parametrize("endpoint", ["decrease-stock", "increase-stock"])
def test_stock_change_without_body_is_rejected(product_service, endpoint):
    """UT-026 (no body at all)"""
    resp = product_service.api.post(f"/products/1/{endpoint}", headers=AUTH)

    assert resp.status_code == 400
    assert resp.get_json() == {"error": "Request body must be a JSON object"}
    product_service.db.cursor.assert_not_called()


@pytest.mark.tc("UT-027")
def test_decrease_stock_for_unknown_product(product_service):
    """UT-027"""
    # Given product 999 does not exist: the UPDATE changes no row (None)
    # and the follow-up SELECT finds nothing either (None)
    product_service.cursor.fetchone.side_effect = [None, None]

    resp = product_service.api.post("/products/999/decrease-stock", json={"quantity": 1}, headers=AUTH)

    assert resp.status_code == 404
    assert resp.get_json() == {"error": "Product not found"}


@pytest.mark.tc("UT-027")
def test_increase_stock_for_unknown_product(product_service):
    """UT-027"""
    # Given the UPDATE matches no row
    product_service.cursor.fetchone.return_value = None

    resp = product_service.api.post("/products/999/increase-stock", json={"quantity": 1}, headers=AUTH)

    assert resp.status_code == 404
    assert resp.get_json() == {"error": "Product not found"}
