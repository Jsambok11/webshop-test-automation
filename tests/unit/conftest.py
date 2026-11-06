"""Fixtures for unit tests. Each fixture gives a fresh, isolated service.

See support.py for how the database and other services are faked.
"""
import pytest

from support import start_service


@pytest.fixture
def user_service(monkeypatch):
    return start_service("user-service", monkeypatch)


@pytest.fixture
def product_service(monkeypatch):
    return start_service("product-service", monkeypatch)


@pytest.fixture
def order_service(monkeypatch):
    return start_service("order-service", monkeypatch)


@pytest.fixture
def frontend(monkeypatch):
    return start_service("frontend-service", monkeypatch)


@pytest.fixture
def make_service(monkeypatch):
    """Factory for tests that need special settings or several services:
    make_service("frontend-service", RATE_LIMIT_ENABLED="true")."""
    return lambda name, **env: start_service(name, monkeypatch, **env)
