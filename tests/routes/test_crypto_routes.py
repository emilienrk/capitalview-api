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
        "symbol": "BTC",
        "type": "BUY",
        "amount": "0.1",
        "price_per_unit": "30000",
        "executed_at": "2023-01-01T12:00:00",
    }
    r = client.post("/crypto/transactions", json=tx)
    assert r.status_code == 201
    created = r.json()
    assert created["symbol"] == "BTC"
    assert "fees" not in created
    assert "fees_symbol" not in created


@patch("services.crypto_transaction.get_crypto_info")
@patch("services.crypto_transaction.get_crypto_price")
def test_crypto_summary(mock_price, mock_info):
    mock_info.return_value = ("Bitcoin", Decimal("40000"))
    mock_price.return_value = Decimal("40000")

    client = TestClient(app)
    resp = client.post("/crypto/accounts", json={"name": "Wallet2"})
    assert resp.status_code == 201
    account_id = resp.json()["id"]

    tx = {
        "account_id": account_id,
        "symbol": "BTC",
        "type": "BUY",
        "amount": "1",
        "price_per_unit": "30000",
        "executed_at": "2023-01-01T12:00:00",
    }
    client.post("/crypto/transactions", json=tx)

    r = client.get(f"/crypto/accounts/{account_id}")
    assert r.status_code == 200
    summary = r.json()
    pos = next(p for p in summary["positions"] if p["symbol"] == "BTC")
    assert Decimal(str(pos["total_amount"])) == Decimal("1")
    assert Decimal(str(pos["current_price"])) == Decimal("40000")


def test_crypto_transactions_crud_and_bulk(session, master_key):
    client = TestClient(app)
    racc = client.post("/crypto/accounts", json={"name": "BulkWallet"})
    assert racc.status_code == 201
    account_id = racc.json()["id"]

    tx = {
        "account_id": account_id,
        "symbol": "ETH",
        "type": "BUY",
        "amount": "2",
        "price_per_unit": "2000",
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

    rupd = client.put(f"/crypto/transactions/{tx_id}", json={"symbol": "ETHX", "amount": "1"})
    assert rupd.status_code == 200

    rdel = client.delete(f"/crypto/transactions/{tx_id}")
    assert rdel.status_code == 204

    bulk = {
        "account_id": account_id,
        "transactions": [
            {"symbol": "BTC", "type": "BUY", "amount": "0.1", "price_per_unit": "30000", "executed_at": "2023-01-01T00:00:00"},
            {"symbol": "ETH", "type": "BUY", "amount": "5", "price_per_unit": "2000", "executed_at": "2023-01-01T00:00:00"}
        ]
    }
    rbulk = client.post("/crypto/transactions/bulk", json=bulk)
    assert rbulk.status_code == 201
    assert rbulk.json()["imported_count"] == 2


def test_create_crypto_transaction_negative_validation(session, master_key):
    client = TestClient(app)
    
    # Create account
    resp = client.post("/crypto/accounts", json={"name": "Test Wallet"})
    account_id = resp.json()["id"]

    # Negative amount
    tx_neg_amount = {
        "account_id": account_id,
        "symbol": "BTC",
        "type": "BUY",
        "amount": -0.1,
        "price_per_unit": 30000,
        "executed_at": "2023-01-01T12:00:00"
    }
    r = client.post("/crypto/transactions", json=tx_neg_amount)
    assert r.status_code == 422

    # Negative price
    tx_neg_price = {
        "account_id": account_id,
        "symbol": "BTC",
        "type": "BUY",
        "amount": 0.1,
        "price_per_unit": -30000,
        "executed_at": "2023-01-01T12:00:00"
    }
    r = client.post("/crypto/transactions", json=tx_neg_price)
    assert r.status_code == 422


def test_composite_transaction_eur(session, master_key):
    """Buying BTC with EUR via composite endpoint returns 2 rows: BUY BTC + SPEND EUR."""
    client = TestClient(app)
    resp = client.post("/crypto/accounts", json={"name": "Composite Wallet"})
    assert resp.status_code == 201
    account_id = resp.json()["id"]

    payload = {
        "account_id": account_id,
        "symbol": "BTC",
        "type": "BUY",
        "amount": "0.1",
        "eur_amount": "3000",
        "executed_at": "2023-06-01T10:00:00",
        "quote_symbol": "EUR",
        "quote_amount": "3000",
    }
    r = client.post("/crypto/transactions/composite", json=payload)
    assert r.status_code == 201
    rows = r.json()
    assert isinstance(rows, list)
    assert len(rows) == 2  # BUY BTC + SPEND EUR
    types = {row["type"] for row in rows}
    assert types == {"BUY", "SPEND"}
    assert all(row["group_uuid"] is not None for row in rows)
    assert len({row["group_uuid"] for row in rows}) == 1


def test_composite_transaction_crypto_quote(session, master_key):
    """Swapping USDC for BTC via composite endpoint returns 3 rows.
    FIAT_ANCHOR carries the EUR value."""
    client = TestClient(app)
    resp = client.post("/crypto/accounts", json={"name": "Swap Wallet"})
    account_id = resp.json()["id"]

    payload = {
        "account_id": account_id,
        "symbol": "BTC",
        "type": "BUY",
        "amount": "0.1",
        "eur_amount": "2760",
        "executed_at": "2023-06-02T10:00:00",
        "quote_symbol": "USDC",
        "quote_amount": "3000",
    }
    r = client.post("/crypto/transactions/composite", json=payload)
    assert r.status_code == 201
    rows = r.json()
    assert len(rows) == 3  # BUY + SPEND + FIAT_ANCHOR
    types = {row["type"] for row in rows}
    assert types == {"BUY", "SPEND", "FIAT_ANCHOR"}
    # All rows share the same group_uuid
    assert len({row["group_uuid"] for row in rows}) == 1
