"""
Route tests for POST /banking/transfer-decisions and
GET /banking/transactions/{id}/counterparts.
"""
import pytest
from fastapi.testclient import TestClient

from main import app
from models.user import User
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


def _month(client: TestClient) -> dict[str, dict]:
    response = client.get("/banking/transactions?period=2026-03")
    assert response.status_code == 200
    return {tx["label"]: tx for tx in response.json()["transactions"]}


def _seed(session, master_key) -> None:
    _link(session, master_key, "current")
    _link(session, master_key, "neobank")
    _store(session, master_key, "neobank", _raw("50.00", "DBIT", "2026-03-16", ref="out", label="out"))
    _store(session, master_key, "current", _raw("50.00", "CRDT", "2026-03-23", ref="in", label="in"))


def test_a_bound_transfer_shows_on_the_month(client, session, master_key):
    _seed(session, master_key)
    month = _month(client)

    counterparts = client.get(f"/banking/transactions/{month['out']['id']}/counterparts")
    assert [tx["label"] for tx in counterparts.json()] == ["in"]

    response = client.post(
        "/banking/transfer-decisions",
        json={"transaction_id": month["in"]["id"], "other_transaction_id": month["out"]["id"], "kind": "transfer"},
    )
    assert response.status_code == 204
    after = _month(client)
    assert after["out"]["transfer_status"] == "confirmed"
    assert after["out"]["transfer_id"] == month["in"]["id"]


def test_an_impossible_decision_is_a_400(client, session, master_key):
    _seed(session, master_key)
    month = _month(client)
    response = client.post(
        "/banking/transfer-decisions",
        json={"transaction_id": month["in"]["id"], "other_transaction_id": month["out"]["id"], "kind": "reversal"},
    )
    assert response.status_code == 400


def test_an_unknown_operation_is_a_404(client, session, master_key):
    _seed(session, master_key)
    month = _month(client)
    response = client.post(
        "/banking/transfer-decisions",
        json={"transaction_id": month["in"]["id"], "other_transaction_id": "nope", "kind": "transfer"},
    )
    assert response.status_code == 404
    assert client.get("/banking/transactions/nope/counterparts").status_code == 404
