"""
Route tests for /banking/recurring: the decisions and corrections a user
makes, and how each survives what the next rebuild finds.
"""
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from main import app
from models.banking import BankTransaction
from models.user import User
from services.encryption import hash_index
from tests.services.test_banking_flows import USER, _raw, _store
from tests.services.test_banking_real_cashflow import CURRENT, _ops
from tests.services.test_banking_recurring import _months

EDF = "PRLV SEPA EDF clients particuliers"
CLAUDE = "CARTE ANTHROPIC* CLAUDE CB*0837"
# A transfer to a person: never counted unasked, so it carries a question.
POCKET_MONEY = "VIR SEPA MARIE DUPONT"


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


def _items(client: TestClient) -> list[dict]:
    response = client.get("/banking/recurring")
    assert response.status_code == 200
    return response.json()["items"]


def _one(client: TestClient) -> dict:
    [item] = _items(client)
    return item


def _to_confirm(client: TestClient) -> int:
    return client.get("/banking/transfer-questions").json()["recurring"]


def _decide(client: TestClient, transaction_id: str, decision: str, **extra) -> dict | None:
    response = client.post(
        "/banking/recurring/decisions", json={"transaction_id": transaction_id, "decision": decision, **extra},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _ids(client: TestClient, period: str) -> dict[str, str]:
    response = client.get(f"/banking/transactions?period={period}")
    return {tx["label"]: tx["id"] for tx in response.json()["transactions"]}


def test_an_operation_of_another_user_is_not_found(client, session, master_key):
    _store(session, master_key, "stranger", _raw("21.60", "DBIT", "2026-03-02", ref="stranger-1", label=CLAUDE))
    [row] = session.exec(select(BankTransaction)).all()
    assert client.post(
        "/banking/recurring/decisions", json={"transaction_id": row.uuid, "decision": "confirm"},
    ).status_code == 404
    assert client.post("/banking/recurring", json={"transaction_id": row.uuid}).status_code == 404


def test_a_user_without_a_bank_account_reads_an_empty_list(client):
    # Not everyone imports their bank: the tab answers, empty, rather than
    # failing on totals it has nothing to add up.
    body = client.get("/banking/recurring").json()
    assert (body["items"], body["currency"]) == ([], "EUR")
    assert (Decimal(str(body["monthly_total"])), Decimal(str(body["annual_total"]))) == (0, 0)
    assert client.get("/banking/review-queue").json()["recurring_count"] == 0


def test_an_unknown_recurring_payment_is_not_found(client):
    assert client.patch("/banking/recurring/nope", json={"name": "x"}).status_code == 404
    assert client.delete("/banking/recurring/nope").status_code == 404
    assert client.get("/banking/recurring/nope/operations").status_code == 404
    assert client.post("/banking/recurring/nope/merge", json={"other_id": "other"}).status_code == 404


def test_confirmed_the_next_debit_counts_without_asking_again(client, session, master_key):
    _ops(session, master_key, *_months(CURRENT, "2026-01", 3, 2, "21.60", POCKET_MONEY))
    candidate = _one(client)
    assert (candidate["state"], _to_confirm(client)) == ("candidate", 1)

    confirmed = _decide(client, candidate["transaction_id"], "confirm", name="Argent de poche")
    assert (confirmed["state"], confirmed["name"], _to_confirm(client)) == ("confirmed", "Argent de poche", 0)

    _ops(session, master_key, (CURRENT, "2026-04-02", "21.60", "DBIT", POCKET_MONEY))
    item = _one(client)
    assert (item["id"], item["state"], item["occurrence_count"]) == (confirmed["id"], "confirmed", 4)
    april = client.get("/banking/transactions?period=2026-04").json()["transactions"][0]
    assert april["recurring"]["id"] == confirmed["id"] and april["recurring_question"] is None


def test_refused_it_asks_no_more_and_counts_nothing_even_a_month_later(client, session, master_key):
    _ops(session, master_key, *_months(CURRENT, "2026-01", 3, 2, "21.60", CLAUDE))
    refused = _decide(client, _one(client)["transaction_id"], "refuse")
    assert refused["state"] == "refused"

    _ops(session, master_key, (CURRENT, "2026-04-02", "21.60", "DBIT", CLAUDE))
    item = _one(client)
    assert (item["state"], item["occurrence_count"], _to_confirm(client)) == ("refused", 4, 0)
    april = client.get("/banking/transactions?period=2026-04").json()["transactions"][0]
    assert april["recurring"] is None and april["recurring_question"] is None


def test_a_decision_is_found_again_once_its_operations_are_imported_again(client, session, master_key):
    # Two contracts with one supplier: the refusal is about one of them.
    contracts = [*_months(CURRENT, "2025-06", 8, 5, "60.00", EDF), *_months(CURRENT, "2025-06", 8, 20, "15.00", EDF)]
    _ops(session, master_key, *contracts)
    refused = _decide(client, next(i for i in _items(client) if Decimal(i["amount"]) == 60)["transaction_id"], "refuse")

    for row in session.exec(select(BankTransaction)).all():
        session.delete(row)
    session.commit()
    # Imported again, with the month since: every operation under a new id.
    for n, (account, day, amount, direction, label) in enumerate([*contracts, (CURRENT, "2026-02-05", "60.00", "DBIT", EDF)]):
        again = "61.20" if amount == "60.00" else amount
        _store(session, master_key, account, _raw(again, direction, day, ref=f"again-{n}", label=label))

    assert {Decimal(item["amount"]): (item["id"], item["state"]) for item in _items(client)} == {
        Decimal("61.20"): (refused["id"], "refused"), Decimal("15"): (None, "auto"),
    }


def test_a_decision_holds_through_a_price_its_identity_no_longer_matches(client, session, master_key):
    _ops(session, master_key, *_months(CURRENT, "2025-06", 8, 5, "60.00", EDF))
    confirmed = _decide(client, _one(client)["transaction_id"], "confirm")

    _ops(session, master_key, *_months(CURRENT, "2026-02", 3, 5, "90.00", EDF))

    item = _one(client)
    assert (item["id"], Decimal(item["amount"]), item["occurrence_count"]) == (confirmed["id"], 90, 11)


def test_an_annual_charge_seen_once_is_marked_by_hand(client, session, master_key):
    _ops(
        session, master_key,
        (CURRENT, "2026-03-10", "89.00", "DBIT", "CARTE DECATHLON CB*0837"),
        # A purchase at the same shop a month later is not a second due date.
        (CURRENT, "2026-04-10", "12.00", "DBIT", "CARTE DECATHLON CB*0837"),
    )
    [operation] = _ids(client, "2026-03").values()

    response = client.post("/banking/recurring", json={"transaction_id": operation, "cadence": "annual"})

    assert response.status_code == 201
    body = response.json()
    assert (body["state"], body["cadence"], body["occurrence_count"], body["next_date"]) == (
        "confirmed", "annual", 1, "2027-03-10",
    )


def test_a_credit_counted_as_income_is_marked_as_a_recurring_income(client, session, master_key):
    _ops(session, master_key, (CURRENT, "2026-03-10", "1850.00", "CRDT", "VIR SEPA VILMORIN SALAIRE"))
    [operation] = _ids(client, "2026-03").values()

    response = client.post("/banking/recurring", json={"transaction_id": operation})

    assert response.status_code == 201
    assert (response.json()["direction"], response.json()["state"]) == ("income", "confirmed")
    # Listed with the income, never among the payments.
    assert _items(client) == []
    [item] = client.get("/banking/recurring?direction=income").json()["items"]
    assert item["id"] == response.json()["id"]


def test_a_credit_counted_as_an_expense_cannot_be_marked(client, session, master_key):
    _ops(session, master_key, (CURRENT, "2026-03-10", "25.00", "CRDT", "AVOIR AMAZON EU"))
    [operation] = _ids(client, "2026-03").values()
    client.put(f"/banking/transactions/{operation}/type", json={"type": "EXPENSE", "scope": "operation"})

    assert client.post("/banking/recurring", json={"transaction_id": operation}).status_code == 409


def test_a_debit_under_a_new_name_can_be_attached(client, session, master_key):
    _ops(
        session, master_key,
        *_months(CURRENT, "2025-06", 8, 5, "60.00", EDF),
        # The same contract under another name, and at another price.
        (CURRENT, "2026-02-05", "64.00", "DBIT", "PRLV SEPA ELECTRICITE DE FRANCE"),
    )
    stored = _decide(client, _one(client)["transaction_id"], "confirm")
    renamed = _ids(client, "2026-02")["PRLV SEPA ELECTRICITE DE FRANCE"]

    response = client.post(
        f"/banking/recurring/{stored['id']}/operations", json={"transaction_id": renamed, "action": "include"},
    )

    assert response.status_code == 200
    assert response.json()["occurrence_count"] + response.json()["extra_count"] == 8
    members = client.get(f"/banking/recurring/{stored['id']}/operations").json()
    assert renamed in {tx["id"] for tx in members}
    assert next(tx for tx in members if tx["id"] == renamed)["recurring"]["role"] == "manual"


def test_a_detached_debit_no_longer_counts(client, session, master_key):
    _ops(session, master_key, *_months(CURRENT, "2025-06", 8, 5, "60.00", EDF))
    stored = _decide(client, _one(client)["transaction_id"], "confirm")
    november = _ids(client, "2025-11")[EDF]

    client.post(f"/banking/recurring/{stored['id']}/operations", json={"transaction_id": november, "action": "exclude"})

    assert _one(client)["occurrence_count"] == 7
    assert client.get("/banking/transactions?period=2025-11").json()["transactions"][0]["recurring"] is None


def test_two_recurring_payments_merge_into_one(client, session, master_key):
    _ops(
        session, master_key,
        *_months(CURRENT, "2025-01", 8, 27, "11.99", "PRLV SEPA ORANGE SA"),
        *_months(CURRENT, "2025-09", 8, 16, "16.99", "PRLV SEPA BOUYGUES TELECOM"),
    )
    orange, bouygues = sorted(_items(client), key=lambda item: item["first_date"])
    confirmed = _decide(client, orange["transaction_id"], "confirm")

    response = client.post(
        f"/banking/recurring/{confirmed['id']}/merge", json={"other_transaction_id": bouygues["transaction_id"]},
    )

    assert response.status_code == 200
    merged = _one(client)
    assert (merged["id"], merged["occurrence_count"], merged["name"]) == (confirmed["id"], 16, "Bouygues Telecom")


def test_forgetting_a_decision_asks_again(client, session, master_key):
    _ops(session, master_key, *_months(CURRENT, "2026-01", 3, 2, "21.60", POCKET_MONEY))
    refused = _decide(client, _one(client)["transaction_id"], "refuse")

    assert client.delete(f"/banking/recurring/{refused['id']}").status_code == 204

    assert (_one(client)["state"], _one(client)["id"], _to_confirm(client)) == ("candidate", None, 1)


def test_renamed_and_ended_on(client, session, master_key):
    _ops(session, master_key, *_months(CURRENT, "2025-06", 8, 5, "60.00", EDF))
    stored = _decide(client, _one(client)["transaction_id"], "confirm")

    response = client.patch(
        f"/banking/recurring/{stored['id']}", json={"name": "Électricité", "ended_on": "2026-01-20"},
    )

    body = response.json()
    assert (body["name"], body["ended_on"], body["status"]) == ("Électricité", "2026-01-20", "ended")
    cleared = client.patch(f"/banking/recurring/{stored['id']}", json={"ended_on": None}).json()
    assert (cleared["name"], cleared["ended_on"]) == ("Électricité", None)


def test_the_nature_is_the_user_s_alone(client, session, master_key):
    _ops(session, master_key, *_months(CURRENT, "2025-06", 8, 5, "60.00", EDF))
    unfiled = _one(client)
    assert unfiled["nature"] is None

    stored = _decide(client, unfiled["transaction_id"], "confirm")
    filed = client.patch(f"/banking/recurring/{stored['id']}", json={"nature": "energy"}).json()
    assert filed["nature"] == "energy"
    assert _one(client)["nature"] == "energy"

    cleared = client.patch(f"/banking/recurring/{stored['id']}", json={"nature": None}).json()
    assert cleared["nature"] is None


def test_nothing_about_the_operations_sits_in_clear(client, session, master_key):
    _ops(session, master_key, *_months(CURRENT, "2025-06", 8, 5, "60.00", EDF))
    _decide(client, _one(client)["transaction_id"], "confirm")
    from models.banking import BankRecurringSeries

    [row] = session.exec(select(BankRecurringSeries)).all()
    stored = " ".join(str(value) for value in row.model_dump().values())
    for tx in session.exec(select(BankTransaction)).all():
        assert tx.uuid not in stored and hash_index(tx.uuid, master_key) not in stored


def test_a_refund_detached_from_one_recurring_payment_stays_another_s(client, session, master_key):
    _ops(
        session, master_key,
        *_months(CURRENT, "2025-06", 8, 5, "60.00", EDF),
        *_months(CURRENT, "2025-06", 8, 20, "15.00", EDF),
        (CURRENT, "2026-01-25", "9.00", "CRDT", "VIR SEPA EDF clients particuliers"),
    )
    items = sorted(_items(client), key=lambda item: Decimal(item["amount"]))
    refund = next(tx for tx in client.get("/banking/transactions?period=2026-01").json()["transactions"] if tx["is_credit"])
    holder = next(item for item in items if item["refunds"]["items"])
    other = next(item for item in items if item is not holder)
    decided = _decide(client, holder["transaction_id"], "confirm")
    _decide(client, other["transaction_id"], "confirm")

    client.post(f"/banking/recurring/{decided['id']}/operations", json={"transaction_id": refund["id"], "action": "exclude"})

    refunds = {item["key"]: [r["id"] for r in item["refunds"]["items"]] for item in _items(client)}
    assert refunds[decided["id"]] == [] and [refund["id"]] in refunds.values()


SALARY = "VIR SEPA VILMORIN & CIE SALAIRE"


def test_income_is_listed_apart_filed_as_income_and_never_merged_with_a_payment(client, session, master_key):
    _ops(
        session, master_key,
        *_months(CURRENT, "2025-06", 8, 28, "1380.71", SALARY, "CRDT"),
        *_months(CURRENT, "2025-06", 8, 5, "60.00", EDF),
    )
    [edf] = _items(client)
    response = client.get("/banking/recurring", params={"direction": "income"})
    assert response.status_code == 200
    [salary] = response.json()["items"]
    assert (response.json()["direction"], salary["direction"], edf["direction"]) == ("income", "income", "expense")

    decided = _decide(client, salary["transaction_id"], "confirm")
    assert client.patch(f"/banking/recurring/{decided['id']}", json={"nature": "salary"}).json()["nature"] == "salary"
    assert client.patch(f"/banking/recurring/{decided['id']}", json={"nature": "housing"}).status_code == 422
    merged = client.post(f"/banking/recurring/{decided['id']}/merge", json={"other_transaction_id": edf["transaction_id"]})
    assert merged.status_code == 409
