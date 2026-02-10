from decimal import Decimal
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from main import app
from models.user import User


@pytest.fixture(autouse=True)
def _override_deps(session, master_key):
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


def test_crypto_account_and_transaction(session, master_key):
    client = TestClient(app)

    resp = client.post("/crypto/accounts", json={"name": "Wallet", "platform": "Binance"})
    assert resp.status_code == 201
    acc = resp.json()
    account_id = acc["id"]

    tx = {
        "account_id": account_id,
        "ticker": "BTC",
        "type": "BUY",
        "amount": "0.1",
        "price_per_unit": "30000",
        "fees": "1",
        "fees_ticker": "EUR",
        "executed_at": "2023-01-01T12:00:00",
    }
    r = client.post("/crypto/transactions", json=tx)
    assert r.status_code == 201
    created = r.json()
    assert created["ticker"] == "BTC"


@patch("services.crypto_transaction.get_market_info")
@patch("services.crypto_transaction.get_market_price")
def test_crypto_summary(mock_price, mock_market, session, master_key):
    mock_market.return_value = ("Bitcoin", Decimal("40000"))
    mock_price.return_value = Decimal("40000")

    client = TestClient(app)
    resp = client.post("/crypto/accounts", json={"name": "Wallet2"})
    assert resp.status_code == 201
    account_id = resp.json()["id"]

    tx = {
        "account_id": account_id,
        "ticker": "BTC",
        "type": "BUY",
        "amount": "1",
        "price_per_unit": "30000",
        "fees": "10",
        "fees_ticker": "EUR",
        "executed_at": "2023-01-01T12:00:00",
    }
    client.post("/crypto/transactions", json=tx)

    r = client.get(f"/crypto/accounts/{account_id}")
    assert r.status_code == 200
    summary = r.json()
    pos = next(p for p in summary["positions"] if p["ticker"] == "BTC")
    assert Decimal(str(pos["total_amount"])) == Decimal("1")
    assert Decimal(str(pos["current_price"])) == Decimal("40000")


def test_crypto_transactions_crud_and_bulk(session, master_key):
    client = TestClient(app)
    racc = client.post("/crypto/accounts", json={"name": "BulkWallet"})
    assert racc.status_code == 201
    account_id = racc.json()["id"]

    tx = {
        "account_id": account_id,
        "ticker": "ETH",
        "type": "BUY",
        "amount": "2",
        "price_per_unit": "2000",
        "fees": "0.01",
        "fees_ticker": "ETH",
        "executed_at": "2023-01-02T12:00:00",
    }
    r1 = client.post("/crypto/transactions", json=tx)
    assert r1.status_code == 201
    created = r1.json()
    tx_id = created["id"]

    rlist = client.get("/crypto/transactions")
    assert rlist.status_code == 200
    assert any(t["id"] == tx_id for t in rlist.json())

    rget = client.get(f"/crypto/transactions/{tx_id}")
    assert rget.status_code == 200

    rupd = client.put(f"/crypto/transactions/{tx_id}", json={"ticker": "ETHX", "amount": "1"})
    assert rupd.status_code == 200

    rdel = client.delete(f"/crypto/transactions/{tx_id}")
    assert rdel.status_code == 204

    bulk = {
        "account_id": account_id,
        "transactions": [
            {"ticker": "BTC", "type": "BUY", "amount": "0.1", "price_per_unit": "30000", "fees": "1", "fees_ticker": "EUR", "executed_at": "2023-01-01T00:00:00"},
            {"ticker": "ETH", "type": "BUY", "amount": "5", "price_per_unit": "2000", "fees": "0", "fees_ticker": None, "executed_at": "2023-01-01T00:00:00"}
        ]
    }
    rbulk = client.post("/crypto/transactions/bulk", json=bulk)
    assert rbulk.status_code == 201
    assert rbulk.json()["imported_count"] == 2
