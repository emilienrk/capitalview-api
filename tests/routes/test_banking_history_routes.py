"""
Route tests for GET /banking/history and DELETE /banking/history/{kind}/{id}:
the user's answers still in force, each one withdrawable.
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


def _op(session, master_key, account: str, day: str, amount: str, direction: str, label: str) -> None:
    _store(session, master_key, account, _raw(amount, direction, day, ref=f"{account}-{day}-{label}", label=label))


def _ops(client: TestClient, period: str = "2026-03") -> dict[str, dict]:
    return {tx["label"]: tx for tx in client.get(f"/banking/transactions?period={period}").json()["transactions"]}


def _history(client: TestClient) -> list[dict]:
    response = client.get("/banking/history")
    assert response.status_code == 200
    return response.json()


def test_answers_are_listed_newest_first_and_each_one_withdrawn(client, session, master_key):
    _link(session, master_key, "current")
    _link(session, master_key, "neobank")
    _op(session, master_key, "current", "2026-03-05", "35.00", "DBIT", "CARTE BOULANGERIE")
    _op(session, master_key, "current", "2026-03-09", "400.00", "CRDT", "VIR INST JEAN TIERS")
    _op(session, master_key, "neobank", "2026-03-16", "50.00", "DBIT", "To Emilien Roukine")
    _op(session, master_key, "current", "2026-03-17", "50.00", "CRDT", "VIR Virement de Emilien ROUKINE")
    ops = _ops(client)
    client.put(f"/banking/transactions/{ops['CARTE BOULANGERIE']['id']}/type", json={"type": "SAVING", "scope": "operation"})
    client.put(f"/banking/transactions/{ops['VIR INST JEAN TIERS']['id']}/type", json={"type": "NEUTRAL", "scope": "label"})
    pair = ops["To Emilien Roukine"]
    client.post("/banking/transfer-decisions", json={
        "transaction_id": pair["id"], "other_transaction_id": pair["transfer_id"], "kind": "transfer",
    })

    history = _history(client)

    assert [item["kind"] for item in history] == ["transfer", "rule", "type"]
    transfer, rule, typed = history
    assert sorted(op["label"] for op in transfer["operations"]) == ["To Emilien Roukine", "VIR Virement de Emilien ROUKINE"]
    assert (rule["type"], rule["operation_count"], rule["name"]) == ("NEUTRAL", 1, "VIR INST JEAN TIERS")
    assert (typed["type"], typed["operations"][0]["label"], typed["overridden_by_pair"]) == ("SAVING", "CARTE BOULANGERIE", False)
    assert typed["at"] is not None

    for item in history:
        assert client.delete(f"/banking/history/{item['kind']}/{item['id']}").status_code == 204
    assert _history(client) == []
    after = _ops(client)
    assert after["CARTE BOULANGERIE"]["type_source"] == "default"
    assert after["VIR INST JEAN TIERS"]["type_source"] == "default"
    assert after["To Emilien Roukine"]["transfer_status"] == "suggested"


def test_a_refused_pair_is_listed_and_withdrawing_it_offers_the_pair_again(client, session, master_key):
    _link(session, master_key, "current")
    _link(session, master_key, "neobank")
    _op(session, master_key, "neobank", "2026-03-16", "50.00", "DBIT", "To Emilien Roukine")
    _op(session, master_key, "current", "2026-03-17", "50.00", "CRDT", "VIR Virement de Emilien ROUKINE")
    pair = _ops(client)["To Emilien Roukine"]
    client.post("/banking/transfer-decisions", json={
        "transaction_id": pair["id"], "other_transaction_id": pair["transfer_id"], "kind": "not_transfer",
    })
    assert _ops(client)["To Emilien Roukine"]["transfer_status"] is None

    [refused] = _history(client)
    assert refused["kind"] == "not_transfer"
    client.delete(f"/banking/history/not_transfer/{refused['id']}")

    assert _ops(client)["To Emilien Roukine"]["transfer_status"] == "suggested"


def test_a_type_a_pair_recognised_since_outranks_says_so(client, session, master_key):
    """Never replaced in silence (docs/bank-sorting.md)."""
    _link(session, master_key, "current")
    _op(session, master_key, "current", "2026-03-05", "300.00", "DBIT", "VIR Virement depuis Compte courant")
    tx = _ops(client)["VIR Virement depuis Compte courant"]
    client.put(f"/banking/transactions/{tx['id']}/type", json={"type": "EXPENSE", "scope": "operation"})
    _link(session, master_key, "savings")  # a Livret A, imported later
    _op(session, master_key, "savings", "2026-03-05", "300.00", "CRDT", "VIR Virement depuis Compte courant")

    [typed] = _history(client)

    assert (typed["kind"], typed["overridden_by_pair"]) == ("type", True)


def test_an_unknown_answer_is_a_404(client, session, master_key):
    _link(session, master_key, "current")
    for kind in ("transfer", "type", "rule", "recurring"):
        assert client.delete(f"/banking/history/{kind}/nope").status_code == 404
    assert client.delete("/banking/history/elsewhere/nope").status_code == 422
