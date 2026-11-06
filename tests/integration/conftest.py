"""Integration tests. Requires the deployed system (python deploy.py).
The shared fixtures (api, new_user, set_stock, shop, ...) are in tests/system_fixtures.py."""
import pytest


def pytest_collection_modifyitems(items):
    """Tests marked `disruptive` restart the database or redeploy, so they run
    last (in test-case order) and cannot disturb the other tests."""
    def order(item):
        disruptive = item.get_closest_marker("disruptive") is not None
        tc = item.get_closest_marker("tc")
        return (disruptive, tc.args[0] if (disruptive and tc) else "")
    items.sort(key=order)


@pytest.fixture(autouse=True)
def port_forwards_alive(forwards):
    """Before every test: restart any port-forward that kubectl has dropped."""
    forwards.ensure()
