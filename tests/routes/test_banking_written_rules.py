"""
Route tests for the sorting rules written by hand: POST /banking/type-rules,
its preview, and PUT /banking/type-rules/{id} (docs/bank-sorting.md).
"""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from dtos.banking import CashflowType, RuleMatch
from main import app
from models.user import User
from services.banking.type_rules import TypeRule, TypeRules, fold_text
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
    _link(session, master_key, "neobank")
    _op(session, master_key, "current", "2026-03-05", "9.99", "DBIT", "PRLV SEPA Spotify AB 4471")
    _op(session, master_key, "current", "2026-03-12", "45.20", "DBIT", "CARTE 11/03/26 CARREFOUR MARKET")
    _op(session, master_key, "current", "2026-03-20", "12.00", "DBIT", "CARTE 19/03/26 Spotïfy  Family")
    _op(session, master_key, "neobank", "2026-03-08", "9.99", "DBIT", "Spotify")
    _op(session, master_key, "current", "2026-03-28", "5.00", "CRDT", "REMB SPOTIFY")


def _rule(**fields) -> dict:
    return {"match": "contains", "text": "spotify", "account_id": None, "is_credit": False, "type": "NEUTRAL", **fields}


def test_a_contains_rule_on_every_account_types_each_label_holding_its_text(client, session, master_key):
    _seed(session, master_key)

    response = client.post("/banking/type-rules", json=_rule())

    assert response.status_code == 201
    created = response.json()
    assert (created["match"], created["signature"], created["account_id"], created["operation_count"]) == (
        "contains", "spotify", None, 3,
    )
    month = _month(client)
    # Case, accents and runs of spaces folded; a credit is another direction.
    for label in ("PRLV SEPA Spotify AB 4471", "CARTE 19/03/26 Spotïfy  Family", "Spotify"):
        assert (month[label]["cashflow_type"], month[label]["type_source"]) == ("NEUTRAL", "rule")
        assert month[label]["type_rule_id"] == created["id"]
    assert month["REMB SPOTIFY"]["type_source"] != "rule"
    assert month["CARTE 11/03/26 CARREFOUR MARKET"]["type_source"] != "rule"


def test_a_starts_with_rule_kept_to_one_account_reaches_only_there(client, session, master_key):
    _seed(session, master_key)

    response = client.post(
        "/banking/type-rules", json=_rule(match="starts_with", text="carte", account_id="current", type="SAVING"),
    )

    assert response.status_code == 201
    month = _month(client)
    assert month["CARTE 11/03/26 CARREFOUR MARKET"]["cashflow_type"] == "SAVING"
    assert month["CARTE 19/03/26 Spotïfy  Family"]["cashflow_type"] == "SAVING"
    assert month["PRLV SEPA Spotify AB 4471"]["type_source"] != "rule"


def test_the_preview_counts_what_a_rule_would_type_and_what_keeps_the_rest(client, session, master_key):
    _seed(session, master_key)
    month = _month(client)
    client.put(
        f"/banking/transactions/{month['Spotify']['id']}/type", json={"type": "EXPENSE", "scope": "operation"},
    )

    response = client.post("/banking/type-rules/preview", json=_rule())

    assert response.status_code == 200
    preview = response.json()
    assert preview["pattern"] == "spotify"
    assert preview["touched_count"] == 2
    # Newest first.
    assert [op["label"] for op in preview["touched"]] == ["CARTE 19/03/26 Spotïfy  Family", "PRLV SEPA Spotify AB 4471"]
    assert preview["touched"][0]["cashflow_type"] == "EXPENSE"
    # Both are expenses until now: the rule makes them NEUTRAL.
    assert preview["changed_count"] == 2
    # The choice made by hand holds, and is said: the rule would have counted it otherwise.
    assert (preview["overridden_count"], [op["label"] for op in preview["overridden"]]) == (1, ["Spotify"])
    assert (preview["kept_count"], preview["kept"]) == (1, [])
    assert preview["side_count"] == 4
    assert client.get("/banking/type-rules").json() == []


def test_a_choice_by_hand_of_the_rules_own_type_is_not_worth_saying(client, session, master_key):
    _seed(session, master_key)
    spotify = _month(client)["Spotify"]
    client.put(f"/banking/transactions/{spotify['id']}/type", json={"type": "NEUTRAL", "scope": "operation"})

    preview = client.post("/banking/type-rules/preview", json=_rule()).json()

    assert (preview["overridden_count"], preview["kept"]) == (0, [{"reason": "override", "count": 1}])


def test_saved_with_take_overrides_the_rule_takes_the_choices_made_by_hand(client, session, master_key):
    _seed(session, master_key)
    spotify = _month(client)["Spotify"]
    client.put(f"/banking/transactions/{spotify['id']}/type", json={"type": "EXPENSE", "scope": "operation"})

    client.post("/banking/type-rules", json=_rule())
    assert _month(client)["Spotify"]["type_source"] == "override"

    client.post("/banking/type-rules", json=_rule(take_overrides=True))
    assert (_month(client)["Spotify"]["type_source"], _month(client)["Spotify"]["cashflow_type"]) == ("rule", "NEUTRAL")
    assert client.get("/banking/history").json() == []


def test_the_preview_names_the_rules_it_takes_from_and_yields_to(client, session, master_key):
    _seed(session, master_key)
    broad = client.post("/banking/type-rules", json=_rule(text="carte", type="EXPENSE")).json()
    precise = client.post("/banking/type-rules", json=_rule(text="carte 11/03", type="SAVING")).json()

    preview = client.post("/banking/type-rules/preview", json=_rule(text="carte 19/03/26 spot")).json()

    assert preview["touched_count"] == 1
    assert [(rule["id"], rule["match"], rule["count"]) for rule in preview["taken"]] == [(broad["id"], "contains", 1)]
    assert preview["yielded"] == []

    # The same rule again, another type: it replaces the broad one rather than taking from it.
    preview = client.post("/banking/type-rules/preview", json=_rule(text="carte")).json()

    assert (preview["touched_count"], preview["taken"]) == (1, [])
    assert [(rule["id"], rule["signature"], rule["count"]) for rule in preview["yielded"]] == [
        (precise["id"], "carte 11/03", 1),
    ]

def test_a_rule_given_from_an_answer_outranks_a_written_one(client, session, master_key):
    _seed(session, master_key)
    month = _month(client)
    client.put(
        f"/banking/transactions/{month['PRLV SEPA Spotify AB 4471']['id']}/type",
        json={"type": "EXPENSE", "scope": "label"},
    )

    preview = client.post("/banking/type-rules/preview", json=_rule()).json()

    assert (preview["touched_count"], preview["kept"]) == (2, [{"reason": "rule", "count": 1}])
    client.post("/banking/type-rules", json=_rule())
    assert _month(client)["PRLV SEPA Spotify AB 4471"]["cashflow_type"] == "EXPENSE"


def test_editing_a_rule_rewrites_it_under_a_new_id(client, session, master_key):
    _seed(session, master_key)
    month = _month(client)
    client.put(
        f"/banking/transactions/{month['PRLV SEPA Spotify AB 4471']['id']}/type",
        json={"type": "EXPENSE", "scope": "label"},
    )
    [answered] = client.get("/banking/type-rules").json()

    # The rule being edited does not stand in its own way.
    preview = client.post(f"/banking/type-rules/preview?replacing={answered['id']}", json=_rule()).json()
    assert (preview["touched_count"], preview["kept_count"]) == (3, 0)

    response = client.put(f"/banking/type-rules/{answered['id']}", json=_rule())

    assert response.status_code == 200
    rewritten = response.json()
    assert rewritten["id"] != answered["id"]
    assert [rule["id"] for rule in client.get("/banking/type-rules").json()] == [rewritten["id"]]
    assert _month(client)["PRLV SEPA Spotify AB 4471"]["cashflow_type"] == "NEUTRAL"
    assert client.put(f"/banking/type-rules/{answered['id']}", json=_rule()).status_code == 404


def test_writing_the_same_rule_again_replaces_it(client, session, master_key):
    _seed(session, master_key)
    client.post("/banking/type-rules", json=_rule())
    client.post("/banking/type-rules", json=_rule(text="SPOTIFY ", type="EXPENSE"))

    [rule] = client.get("/banking/type-rules").json()

    assert (rule["signature"], rule["type"]) == ("spotify", "EXPENSE")


def test_a_pair_still_outranks_a_written_rule(client, session, master_key):
    _link(session, master_key, "current")
    _link(session, master_key, "savings")
    _op(session, master_key, "current", "2026-03-05", "300.00", "DBIT", "VIR Virement vers Livret A")
    _op(session, master_key, "savings", "2026-03-05", "300.00", "CRDT", "VIR Virement depuis Compte courant")

    preview = client.post("/banking/type-rules/preview", json=_rule(text="virement", type="EXPENSE")).json()

    assert (preview["touched_count"], preview["kept"]) == (0, [{"reason": "pair", "count": 1}])


def test_a_text_too_short_or_an_unknown_account_is_refused(client, session, master_key):
    _seed(session, master_key)

    short = client.post("/banking/type-rules", json=_rule(text=" a  b"))
    assert short.status_code == 422
    assert short.json()["detail"] == "Écrivez au moins 3 caractères."
    assert client.post("/banking/type-rules/preview", json=_rule(account_id="elsewhere")).status_code == 404
    assert client.post("/banking/type-rules", json=_rule(match="similar")).status_code == 422
    assert client.get("/banking/type-rules").json() == []


def test_written_rules_rank_by_text_length_then_account_then_date():
    def rule(uuid: str, text: str, account: str | None, second: int) -> TypeRule:
        return TypeRule(
            uuid=uuid, account_bidx=account, is_credit=False, signature=text, words=frozenset(),
            type=CashflowType.EXPENSE, created_at=datetime(2026, 10, 10, 12, 0, second, tzinfo=timezone.utc),
            match=RuleMatch.CONTAINS,
        )

    rules = TypeRules(written=[rule("short", "amazon", None, 3), rule("long", "amazon prime", None, 1)])
    assert rules.reach("acc", False, "AMAZON PRIME FR", frozenset()).uuid == "long"
    assert rules.reach("acc", False, "AMAZON MARKETPLACE", frozenset()).uuid == "short"
    assert rules.reach("acc", True, "AMAZON MARKETPLACE", frozenset()) is None

    tied = TypeRules(written=[rule("everywhere", "amazon", None, 5), rule("here", "amazon", "acc", 1)])
    assert tied.reach("acc", False, "amazon", frozenset()).uuid == "here"
    assert tied.reach("other", False, "amazon", frozenset()).uuid == "everywhere"


def test_a_written_text_is_matched_as_typed_never_as_a_pattern():
    rule = TypeRule(
        uuid="r", account_bidx=None, is_credit=False, signature=fold_text("PAYPAL *STEAM"), words=frozenset(),
        type=CashflowType.EXPENSE, created_at=datetime.now(timezone.utc), match=RuleMatch.STARTS_WITH,
    )
    assert rule.matches(fold_text("Paypal   *Steam Games"))
    assert not rule.matches(fold_text("PAYPAL STEAM"))
