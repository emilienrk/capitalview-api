"""
Route tests for PUT and DELETE /banking/transactions/{id}/type and
/banking/type-rules.
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


def _month(client: TestClient, period: str = "2026-03") -> dict[str, dict]:
    response = client.get(f"/banking/transactions?period={period}")
    assert response.status_code == 200
    return {tx["label"]: tx for tx in response.json()["transactions"]}


def _op(session, master_key, account: str, day: str, amount: str, direction: str, label: str) -> None:
    _store(session, master_key, account, _raw(amount, direction, day, ref=f"{account}-{day}-{label}", label=label))


def _seed(session, master_key) -> None:
    _link(session, master_key, "current")
    _op(session, master_key, "current", "2026-03-05", "400.00", "DBIT", "VIR INST ROUKINE EMILIEN")
    _op(session, master_key, "current", "2026-03-19", "150.00", "DBIT", "VIR INST ROUKINE EMILIEN")
    _op(session, master_key, "current", "2026-03-21", "35.00", "DBIT", "CARTE 20/03/26 BOULANGERIE CB*08")


def test_a_label_correction_types_every_operation_like_it_even_one_imported_later(client, session, master_key):
    _seed(session, master_key)
    target = _month(client)["VIR INST ROUKINE EMILIEN"]

    response = client.put(f"/banking/transactions/{target['id']}/type", json={"type": "SAVING", "scope": "label"})

    assert response.status_code == 200
    body = response.json()
    assert (body["covered_count"], body["transaction"]["cashflow_type"], body["transaction"]["type_source"]) == (2, "SAVING", "rule")
    _op(session, master_key, "current", "2026-04-02", "90.00", "DBIT", "VIR INST ROUKINE EMILIEN")
    assert _month(client, "2026-04")["VIR INST ROUKINE EMILIEN"]["cashflow_type"] == "SAVING"
    assert _month(client)["CARTE 20/03/26 BOULANGERIE CB*08"]["cashflow_type"] == "EXPENSE"


def test_an_operation_correction_types_it_alone_and_can_be_dropped(client, session, master_key):
    _seed(session, master_key)
    target = _month(client)["CARTE 20/03/26 BOULANGERIE CB*08"]

    response = client.put(f"/banking/transactions/{target['id']}/type", json={"type": "INVESTMENT", "scope": "operation"})

    assert (response.status_code, response.json()["transaction"]["type_source"]) == (200, "override")
    dropped = client.delete(f"/banking/transactions/{target['id']}/type")
    assert (dropped.status_code, dropped.json()["cashflow_type"], dropped.json()["type_source"]) == (200, "EXPENSE", "default")


def test_a_label_correction_drops_the_operation_s_own_override(client, session, master_key):
    _seed(session, master_key)
    target = _month(client)["CARTE 20/03/26 BOULANGERIE CB*08"]
    client.put(f"/banking/transactions/{target['id']}/type", json={"type": "INVESTMENT", "scope": "operation"})

    response = client.put(f"/banking/transactions/{target['id']}/type", json={"type": "NEUTRAL", "scope": "label"})

    assert (response.json()["transaction"]["cashflow_type"], response.json()["transaction"]["type_source"]) == ("NEUTRAL", "rule")


def test_a_paired_operation_is_a_409(client, session, master_key):
    _link(session, master_key, "current")
    _link(session, master_key, "savings")  # a Livret A
    _op(session, master_key, "current", "2026-03-05", "300.00", "DBIT", "VIR Virement depuis Compte courant")
    _op(session, master_key, "savings", "2026-03-05", "300.00", "CRDT", "VIR Virement depuis Compte courant")
    [target] = [tx for tx in client.get("/banking/transactions?period=2026-03").json()["transactions"] if not tx["is_credit"]]

    response = client.put(f"/banking/transactions/{target['id']}/type", json={"type": "EXPENSE", "scope": "operation"})

    assert response.status_code == 409


def test_an_operation_without_label_cannot_type_a_label(client, session, master_key):
    _link(session, master_key, "current")
    raw = _raw("12.00", "DBIT", "2026-03-05", ref="bare")
    raw["remittance_information"] = []
    _store(session, master_key, "current", raw)
    [target] = client.get("/banking/transactions?period=2026-03").json()["transactions"]

    assert client.put(f"/banking/transactions/{target['id']}/type", json={"type": "SAVING", "scope": "label"}).status_code == 400
    assert client.put(f"/banking/transactions/{target['id']}/type", json={"type": "SAVING", "scope": "operation"}).status_code == 200


def test_another_user_s_operation_is_a_404(client, session, master_key):
    from services.auth import get_current_user

    _seed(session, master_key)
    target = _month(client)["VIR INST ROUKINE EMILIEN"]
    app.dependency_overrides[get_current_user] = lambda: User(
        uuid="someone-else", auth_salt="salt", username="o", email="o@test", password_hash="x"
    )

    assert client.put(f"/banking/transactions/{target['id']}/type", json={"type": "SAVING"}).status_code == 404
    assert client.delete(f"/banking/transactions/{target['id']}/type").status_code == 404


def test_rules_are_listed_with_what_they_type_and_can_be_deleted(client, session, master_key):
    from services.auth import get_current_user

    _seed(session, master_key)
    _op(session, master_key, "current", "2026-03-25", "60.00", "DBIT", "VIR INST ROUKINE EMILIEN LIVRET")
    target = _month(client)["VIR INST ROUKINE EMILIEN"]
    client.put(f"/banking/transactions/{target['id']}/type", json={"type": "SAVING", "scope": "label"})

    [rule] = client.get("/banking/type-rules").json()
    assert (rule["signature"], rule["label"], rule["type"], rule["operation_count"], rule["is_credit"]) == (
        "emilien inst roukine vir", "VIR INST ROUKINE EMILIEN LIVRET", "SAVING", 3, False,
    )

    owner = app.dependency_overrides[get_current_user]
    app.dependency_overrides[get_current_user] = lambda: User(
        uuid="someone-else", auth_salt="salt", username="o", email="o@test", password_hash="x"
    )
    assert client.delete(f"/banking/type-rules/{rule['id']}").status_code == 404
    app.dependency_overrides[get_current_user] = owner
    assert client.delete(f"/banking/type-rules/{rule['id']}").status_code == 204
    assert client.delete(f"/banking/type-rules/{rule['id']}").status_code == 404
    assert _month(client)["VIR INST ROUKINE EMILIEN"]["cashflow_type"] == "EXPENSE"
