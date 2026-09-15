"""Route tests for GET /banking/real-cashflow and its months."""
from datetime import date

import pytest
from fastapi.testclient import TestClient

from main import app
from models.user import User
from services.banking.flows import _shift_period
from tests.services.test_banking_flows import USER, _link, _raw, _store


@pytest.fixture(autouse=True)
def _override_deps(session, master_key):
    from database import get_session
    from services.auth import get_current_user, get_master_key

    app.dependency_overrides.clear()
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_current_user] = lambda: User(
        uuid=USER, auth_salt="salt", username="t", email="t@test", password_hash="x"
    )
    app.dependency_overrides[get_master_key] = lambda: master_key
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


CURRENT_PERIOD = f"{date.today():%Y-%m}"
LAST_COMPLETED = _shift_period(CURRENT_PERIOD, -1)


def test_a_completed_month_is_served(client, session, master_key):
    _link(session, master_key, "current")
    _store(session, master_key, "current", _raw("40.00", "DBIT", f"{LAST_COMPLETED}-05", ref="r", label="CARTE BOULANGERIE"))

    response = client.get(f"/banking/real-cashflow/months/{LAST_COMPLETED}")

    assert response.status_code == 200
    assert float(response.json()["totals"]["expenses"]) == 40


def test_the_current_month_is_a_400(client):
    assert client.get(f"/banking/real-cashflow/months/{CURRENT_PERIOD}").status_code == 400


def test_a_malformed_period_is_a_422(client):
    assert client.get("/banking/real-cashflow/months/2026-13").status_code == 422


def test_the_year_defaults_to_the_current_one(client, session, master_key):
    response = client.get("/banking/real-cashflow")
    assert response.status_code == 200
    assert response.json()["year"] == date.today().year
