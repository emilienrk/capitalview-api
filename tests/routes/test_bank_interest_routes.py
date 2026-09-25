"""Interest terms on bank accounts, and GET /bank/interest."""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from main import app
from models.user import User


@pytest.fixture(autouse=True)
def _override_deps(session, master_key):
    from database import get_session
    from services.auth import get_current_user, get_master_key

    app.dependency_overrides.clear()
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_current_user] = lambda: User(
        uuid="user_1", auth_salt="salt", username="test", email="t@test", password_hash="x"
    )
    app.dependency_overrides[get_master_key] = lambda: master_key
    yield
    app.dependency_overrides.clear()


def _create(client: TestClient, **fields):
    payload = {"name": "Compte", "account_type": "SAVINGS", "balance": "10000", **fields}
    return client.post("/bank/accounts", json=payload)


def test_a_savings_account_keeps_its_rates_and_counts_by_quinzaine_by_default():
    client = TestClient(app)

    r = _create(client, interest_rate="0.015", boosted_rate="0.05", boosted_until="2026-12-31")

    assert r.status_code == 201, r.text
    data = r.json()
    assert Decimal(data["interest_rate"]) == Decimal("0.015")
    assert Decimal(data["boosted_rate"]) == Decimal("0.05")
    assert data["boosted_until"] == "2026-12-31"
    assert data["interest_method"] == "FORTNIGHTLY"


@pytest.mark.parametrize(
    "fields",
    [
        {"account_type": "CHECKING", "interest_rate": "0.01"},
        {"account_type": "PEL", "interest_rate": "0.02"},
        {"account_type": "LIVRET_A", "interest_rate": "0.024", "interest_method": "DAILY"},
        {"interest_rate": "0.02", "boosted_rate": "0.05"},
        {"boosted_rate": "0.05", "boosted_until": "2026-12-31"},
        {"interest_rate": "1.5"},
    ],
)
def test_interest_terms_the_account_cannot_carry_are_refused(fields):
    assert _create(TestClient(app), **fields).status_code == 422


def test_a_checking_account_reports_no_interest_method():
    r = _create(TestClient(app), account_type="CHECKING")

    assert r.json()["interest_method"] is None
    assert r.json()["interest_rate"] is None


def test_an_update_can_set_then_clear_the_rates():
    client = TestClient(app)
    account_id = _create(client).json()["id"]

    r = client.put(f"/bank/accounts/{account_id}", json={"interest_rate": "0.025", "interest_method": "DAILY"})
    assert r.status_code == 200, r.text
    assert Decimal(r.json()["interest_rate"]) == Decimal("0.025")
    assert r.json()["interest_method"] == "DAILY"

    # A rename leaves the rate alone: only fields sent are written.
    assert Decimal(client.put(f"/bank/accounts/{account_id}", json={"name": "Bourso+"}).json()["interest_rate"]) == Decimal("0.025")

    r = client.put(f"/bank/accounts/{account_id}", json={"interest_rate": None})
    assert r.status_code == 200
    assert r.json()["interest_rate"] is None


def test_clearing_the_base_rate_under_a_boost_is_refused():
    client = TestClient(app)
    account_id = _create(
        client, interest_rate="0.015", boosted_rate="0.05", boosted_until="2026-12-31"
    ).json()["id"]

    r = client.put(f"/bank/accounts/{account_id}", json={"interest_rate": None})

    assert r.status_code == 400
    assert Decimal(client.get(f"/bank/accounts/{account_id}").json()["interest_rate"]) == Decimal("0.015")


def test_interest_is_estimated_only_for_accounts_with_a_rate():
    client = TestClient(app)
    livret_id = _create(client, account_type="LIVRET_A", interest_rate="0.024").json()["id"]
    _create(client, account_type="LDD")
    _create(client, account_type="CHECKING")

    r = client.get("/bank/interest")

    assert r.status_code == 200
    data = r.json()
    assert [d["account_id"] for d in data] == [livret_id]
    assert data[0]["method"] == "FORTNIGHTLY"
    assert Decimal(data[0]["estimated"]) > 0


def test_the_bank_projection_takes_the_declared_rates(session, master_key):
    from services.analytics.projection_basis import derive_projection_defaults

    client = TestClient(app)
    _create(client, account_type="LIVRET_A", balance="10000", interest_rate="0.02")
    _create(client, account_type="SAVINGS", balance="30000", interest_rate="0.04")
    _create(client, account_type="CHECKING", balance="5000")

    bank = derive_projection_defaults(session, "user_1", master_key)["BANK"]

    assert bank.annual_return_rate == pytest.approx(Decimal("0.035"))
    assert bank.return_source == "declared_rates"
    assert [w.code for w in bank.warnings] == ["contribution_not_measured"]
