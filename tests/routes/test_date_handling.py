"""Date/timezone handling: UTC is the single source of truth.

Incoming datetimes: aware values are converted to UTC, naive values are
assumed UTC. Outgoing executed_at is serialized with an explicit UTC marker
so the frontend can convert to the display timezone.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from main import app
from models.user import User


@pytest.fixture(autouse=True)
def _override_deps(session, master_key):
    """Override FastAPI dependencies to use the test DB session and a fake user/master key."""
    def _get_session():
        return session

    def _get_user():
        return User(uuid="user_1", auth_salt="salt", username="test", email="t@test", password_hash="x")

    def _get_master_key():
        return master_key

    app.dependency_overrides.clear()
    from database import get_session
    app.dependency_overrides[get_session] = _get_session
    try:
        from services.auth import get_current_user, get_master_key
        app.dependency_overrides[get_current_user] = _get_user
        app.dependency_overrides[get_master_key] = _get_master_key
    except Exception:
        pass

    yield

    app.dependency_overrides.clear()


def _create_account(client: TestClient) -> str:
    resp = client.post("/stocks/accounts", json={"name": "TZ Account", "account_type": "CTO"})
    assert resp.status_code == 201
    return resp.json()["id"]


def _tx_payload(account_id: str, executed_at: str, amount: str = "1") -> dict:
    return {
        "account_id": account_id,
        "asset_key": "US0378331005",
        "type": "BUY",
        "amount": amount,
        "price_per_unit": "100",
        "fees": "0",
        "executed_at": executed_at,
    }


def test_aware_datetime_converted_to_utc(session, master_key):
    client = TestClient(app)
    account_id = _create_account(client)

    resp = client.post("/stocks/transactions", json=_tx_payload(account_id, "2023-06-15T14:30:00+02:00"))
    assert resp.status_code == 201
    executed_at = resp.json()["executed_at"]

    parsed = datetime.fromisoformat(executed_at.replace("Z", "+00:00"))
    assert parsed.utcoffset() == timedelta(0), f"expected explicit UTC, got {executed_at}"
    assert (parsed.hour, parsed.minute) == (12, 30)


def test_naive_datetime_assumed_utc_and_marked(session, master_key):
    client = TestClient(app)
    account_id = _create_account(client)

    resp = client.post("/stocks/transactions", json=_tx_payload(account_id, "2023-01-01T12:00:00"))
    assert resp.status_code == 201
    executed_at = resp.json()["executed_at"]

    parsed = datetime.fromisoformat(executed_at.replace("Z", "+00:00"))
    assert parsed.utcoffset() == timedelta(0), f"expected explicit UTC, got {executed_at}"
    assert parsed.hour == 12


def test_current_time_with_positive_offset_accepted(session, master_key):
    """A user east of UTC submitting 'now' in their local time must not be rejected."""
    client = TestClient(app)
    account_id = _create_account(client)

    now_paris = datetime.now(timezone(timedelta(hours=2)))
    resp = client.post("/stocks/transactions", json=_tx_payload(account_id, now_paris.isoformat()))
    assert resp.status_code == 201, resp.text


def test_future_datetime_rejected(session, master_key):
    client = TestClient(app)
    account_id = _create_account(client)

    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    resp = client.post("/stocks/transactions", json=_tx_payload(account_id, future))
    assert resp.status_code == 422


def test_pre_2000_datetime_rejected(session, master_key):
    client = TestClient(app)
    account_id = _create_account(client)

    resp = client.post("/stocks/transactions", json=_tx_payload(account_id, "1999-12-31T23:59:59Z"))
    assert resp.status_code == 422


def test_account_transactions_sorted_desc(session, master_key):
    client = TestClient(app)
    account_id = _create_account(client)

    for executed_at in ("2023-05-10T10:00:00Z", "2023-01-01T10:00:00Z", "2023-03-15T10:00:00Z"):
        resp = client.post("/stocks/transactions", json=_tx_payload(account_id, executed_at))
        assert resp.status_code == 201

    resp = client.get(f"/stocks/transactions/account/{account_id}")
    assert resp.status_code == 200
    dates = [tx["executed_at"] for tx in resp.json()]
    parsed = [datetime.fromisoformat(d.replace("Z", "+00:00")) for d in dates]
    assert parsed == sorted(parsed, reverse=True), f"not sorted desc: {dates}"
