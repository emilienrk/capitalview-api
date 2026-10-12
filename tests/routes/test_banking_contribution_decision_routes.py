"""
Route tests for POST /banking/contribution-decisions.
"""
import pytest
from fastapi.testclient import TestClient

from main import app
from models.user import User
from tests.services.test_banking_contributions import _deposit, _pea
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


def _operation(client: TestClient) -> dict:
    response = client.get("/banking/transactions?period=2026-03")
    assert response.status_code == 200
    [tx] = response.json()["transactions"]
    return tx


def _seed(session, master_key) -> None:
    _link(session, master_key, "current")
    _store(session, master_key, "current", _raw("200.00", "DBIT", "2026-03-05", ref="out", label="VIR Virement vers PEA"))
    _pea(session, master_key)
    _deposit(session, master_key, "2026-03-05", "200")


def test_a_yes_counts_the_operation_as_invested(client, session, master_key):
    _seed(session, master_key)
    tx = _operation(client)

    response = client.post("/banking/contribution-decisions", json={
        "transaction_id": tx["id"], "contribution_id": tx["contribution"]["id"], "kind": "contribution",
    })
    assert response.status_code == 204
    after = _operation(client)
    assert (after["cashflow_type"], after["type_source"]) == ("INVESTMENT", "contribution")
    [item] = client.get("/banking/history").json()
    assert (item["kind"], item["name"]) == ("contribution", "PEA")


def test_an_unknown_movement_is_a_404(client, session, master_key):
    _seed(session, master_key)
    tx = _operation(client)
    response = client.post("/banking/contribution-decisions", json={
        "transaction_id": tx["id"], "contribution_id": "nope", "kind": "not_contribution",
    })
    assert response.status_code == 404


def test_a_movement_facing_another_day_is_a_400(client, session, master_key):
    _seed(session, master_key)
    far = _deposit(session, master_key, "2026-03-25", "200")
    tx = _operation(client)
    response = client.post("/banking/contribution-decisions", json={
        "transaction_id": tx["id"], "contribution_id": far.id, "kind": "contribution",
    })
    assert response.status_code == 400
