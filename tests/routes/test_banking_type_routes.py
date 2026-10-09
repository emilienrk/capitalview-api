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


def _seed_asking(session, master_key) -> None:
    """Two credits of one label: a question only the user can answer."""
    _link(session, master_key, "current")
    _op(session, master_key, "current", "2026-03-05", "400.00", "CRDT", "VIR INST ROUKINE EMILIEN")
    _op(session, master_key, "current", "2026-03-19", "150.00", "CRDT", "VIR INST ROUKINE EMILIEN")
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


def _month_all(client: TestClient, period: str = "2026-03") -> list[dict]:
    return client.get(f"/banking/transactions?period={period}").json()["transactions"]


def test_ticked_operations_are_typed_together_and_nothing_else_is(client, session, master_key):
    """Ticked operations ("celles que je coche") are typed one by one: no rule for the next ones."""
    _link(session, master_key, "current")
    for day, amount in (("2026-03-05", "400.00"), ("2026-03-12", "60.00"), ("2026-03-19", "150.00")):
        _op(session, master_key, "current", day, amount, "CRDT", "VIR INST ROUKINE EMILIEN")
    first, second, third = sorted(_month_all(client), key=lambda tx: tx["operation_date"])

    response = client.put(
        f"/banking/transactions/{third['id']}/type",
        json={"type": "SAVING", "scope": "operation", "also": [first["id"], third["id"]]},
    )

    assert (response.status_code, response.json()["covered_count"]) == (200, 2)
    typed = {tx["id"]: (tx["cashflow_type"], tx["type_source"]) for tx in _month_all(client)}
    assert typed[first["id"]] == typed[third["id"]] == ("SAVING", "override")
    assert typed[second["id"]][1] != "override"
    assert client.get("/banking/type-rules").json() == []
    _op(session, master_key, "current", "2026-04-02", "90.00", "CRDT", "VIR INST ROUKINE EMILIEN")
    [later] = _month_all(client, "2026-04")
    assert later["type_source"] != "override" and later["cashflow_type"] != "SAVING"


def test_an_operation_of_another_account_or_direction_cannot_be_ticked(client, session, master_key):
    _seed(session, master_key)
    _link(session, master_key, "neobank")
    _op(session, master_key, "neobank", "2026-03-07", "400.00", "DBIT", "VIR INST ROUKINE EMILIEN")
    _op(session, master_key, "current", "2026-03-08", "20.00", "CRDT", "VIR INST ROUKINE EMILIEN")
    ops = _month_all(client)
    asked = next(tx for tx in ops if tx["operation_date"] == "2026-03-05")
    elsewhere = next(tx for tx in ops if tx["operation_date"] == "2026-03-07")
    credit = next(tx for tx in ops if tx["operation_date"] == "2026-03-08")

    for other in (elsewhere, credit):
        response = client.put(
            f"/banking/transactions/{asked['id']}/type",
            json={"type": "SAVING", "scope": "operation", "also": [other["id"]]},
        )
        assert response.status_code == 400
    assert next(tx for tx in _month_all(client) if tx["id"] == asked["id"])["type_source"] != "override"


def test_a_ticked_paired_operation_is_a_409(client, session, master_key):
    _link(session, master_key, "current")
    _link(session, master_key, "savings")  # a Livret A
    _op(session, master_key, "current", "2026-03-05", "300.00", "DBIT", "VIR Virement depuis Compte courant")
    _op(session, master_key, "savings", "2026-03-05", "300.00", "CRDT", "VIR Virement depuis Compte courant")
    _op(session, master_key, "current", "2026-03-09", "80.00", "DBIT", "VIR Virement depuis Compte courant")
    ops = _month_all(client)
    paired = next(tx for tx in ops if not tx["is_credit"] and float(tx["amount"]) == 300)
    alone = next(tx for tx in ops if float(tx["amount"]) == 80)

    response = client.put(
        f"/banking/transactions/{alone['id']}/type",
        json={"type": "SAVING", "scope": "operation", "also": [paired["id"]]},
    )

    assert response.status_code == 409


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

    # Each word on two debits only: none is common enough to be set aside.
    _link(session, master_key, "current")
    _op(session, master_key, "current", "2026-03-05", "400.00", "DBIT", "VIR INST ROUKINE EMILIEN")
    _op(session, master_key, "current", "2026-03-25", "60.00", "DBIT", "VIR INST ROUKINE EMILIEN LIVRET")
    target = _month(client)["VIR INST ROUKINE EMILIEN"]
    client.put(f"/banking/transactions/{target['id']}/type", json={"type": "SAVING", "scope": "label"})

    [rule] = client.get("/banking/type-rules").json()
    assert (rule["signature"], rule["label"], rule["type"], rule["operation_count"], rule["is_credit"]) == (
        "emilien inst roukine vir", "VIR INST ROUKINE EMILIEN LIVRET", "SAVING", 2, False,
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


def test_flow_questions_count_with_the_transfer_questions_until_answered(client, session, master_key):
    _seed_asking(session, master_key)
    assert client.get("/banking/transfer-questions").json() == {"total": 1, "months": [{"period": "2026-03", "count": 1}], "recurring": 0}
    [target] = [tx for tx in client.get("/banking/transactions?period=2026-03").json()["transactions"] if tx["flow_question"]]
    assert (target["label"], target["flow_question"]["operation_count"]) == ("VIR INST ROUKINE EMILIEN", 2)

    client.put(f"/banking/transactions/{target['id']}/type", json={"type": "SAVING", "scope": "label"})

    assert client.get("/banking/transfer-questions").json() == {"total": 0, "months": [], "recurring": 0}


def test_the_flow_group_route_lists_what_one_answer_would_type(client, session, master_key):
    _seed_asking(session, master_key)
    # The question sits on the label's last operation: read the list itself, which
    # `_month` keys by label and would leave only one operation per label.
    listed = client.get("/banking/transactions?period=2026-03").json()["transactions"]
    [carrier] = [tx for tx in listed if tx["flow_question"]]
    assert carrier["flow_question"]["operation_count"] == 2

    response = client.get(f"/banking/transactions/{carrier['id']}/flow-group")

    assert response.status_code == 200
    assert [(op["operation_date"], op["amount"]) for op in response.json()] == [
        ("2026-03-19", "150"),
        ("2026-03-05", "400"),
    ]


def test_the_flow_group_of_an_unknown_operation_is_a_404(client, session, master_key):
    _seed(session, master_key)
    assert client.get("/banking/transactions/nope/flow-group").status_code == 404
