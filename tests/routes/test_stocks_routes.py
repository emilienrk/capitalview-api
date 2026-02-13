import json
from decimal import Decimal
from unittest.mock import patch

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


def test_create_account_and_transaction(session, master_key):
    client = TestClient(app)

    payload = {
        "name": "Test Account",
        "account_type": "PEA",
        "institution_name": None,
        "identifier": None,
    }
    resp = client.post("/stocks/accounts", json=payload)
    assert resp.status_code == 201
    acc = resp.json()
    assert acc["name"] == "Test Account" or acc.get("name") == "Test Account"

    account_id = acc["id"]

    tx_payload = {
        "account_id": account_id,
        "symbol": "AAPL",
        "isin": "US0378331005",
        "exchange": "NASDAQ",
        "type": "BUY",
        "amount": "2.5",
        "price_per_unit": "150",
        "fees": "1",
        "executed_at": "2023-01-01T12:00:00",
        "notes": "via route",
    }

    resp_tx = client.post("/stocks/transactions", json=tx_payload)
    assert resp_tx.status_code == 201
    tx = resp_tx.json()
    assert tx["symbol"] == "AAPL"

    resp_get = client.get(f"/stocks/transactions/{tx['id']}")
    assert resp_get.status_code == 200
    got = resp_get.json()
    assert got["symbol"] == "AAPL"


@patch("services.stock_transaction.get_stock_info")
def test_account_summary_with_market(mock_market, session, master_key):
    mock_market.return_value = ("Apple Inc.", Decimal("200"))
    client = TestClient(app)

    payload = {"name": "Sum Account", "account_type": "CTO"}
    resp = client.post("/stocks/accounts", json=payload)
    assert resp.status_code == 201
    account_id = resp.json()["id"]

    tx_payload = {
        "account_id": account_id,
        "symbol": "AAPL",
        "isin": "US0378331005",
        "type": "BUY",
        "amount": "1",
        "price_per_unit": "100",
        "fees": "0",
        "executed_at": "2023-01-01T12:00:00",
    }
    r = client.post("/stocks/transactions", json=tx_payload)
    assert r.status_code == 201

    r2 = client.get(f"/stocks/accounts/{account_id}")
    assert r2.status_code == 200
    summary = r2.json()
    assert summary["account_name"] in ("Sum Account", "Sum Account")
    pos = next(p for p in summary["positions"] if p["symbol"] == "AAPL")
    assert Decimal(str(pos["total_amount"])) == Decimal("1")
    assert Decimal(str(pos["current_price"])) == Decimal("200")


def test_stocks_additional_routes(session, master_key):
    client = TestClient(app)

    r1 = client.post("/stocks/accounts", json={"name": "PEA1", "account_type": "PEA"})
    assert r1.status_code == 201
    a1 = r1.json()

    rdup = client.post("/stocks/accounts", json={"name": "PEA2", "account_type": "PEA"})
    assert rdup.status_code == 400

    r2 = client.post("/stocks/accounts", json={"name": "CTO1", "account_type": "CTO"})
    assert r2.status_code == 201
    rlist = client.get("/stocks/accounts")
    assert rlist.status_code == 200
    assert any(acc["name"] == "CTO1" for acc in rlist.json())

    acc_id = r2.json()["id"]
    rupd = client.put(f"/stocks/accounts/{acc_id}", json={"name": "CTO Updated"})
    assert rupd.status_code == 200
    assert rupd.json()["name"] == "CTO Updated"

    tx = {"account_id": acc_id, "symbol": "XYZ", "isin": "ISIN_XYZ", "type": "BUY", "amount": "2", "price_per_unit": "10", "fees": "0", "executed_at": "2023-01-01T00:00:00"}
    rtx = client.post("/stocks/transactions", json=tx)
    assert rtx.status_code == 201
    txid = rtx.json()["id"]

    rlisttx = client.get("/stocks/transactions")
    assert rlisttx.status_code == 200
    assert any(t["id"] == txid for t in rlisttx.json())

    rbyacc = client.get(f"/stocks/transactions/account/{acc_id}")
    assert rbyacc.status_code == 200

    bulk = {"account_id": acc_id, "transactions": [{"symbol": "A", "isin": "ISIN_A", "type": "BUY", "amount": "1", "price_per_unit": "1", "fees": "0", "executed_at": "2023-01-01T00:00:00"}]}
    rbulk = client.post("/stocks/transactions/bulk", json=bulk)
    assert rbulk.status_code == 201
    assert rbulk.json()["imported_count"] == 1

    rdel = client.delete(f"/stocks/accounts/{acc_id}")
    assert rdel.status_code == 204


def test_create_stock_transaction_negative_validation(session, master_key):
    client = TestClient(app)
    
    # Create account first
    resp = client.post("/stocks/accounts", json={"name": "Test Acc", "account_type": "CTO"})
    account_id = resp.json()["id"]

    # Negative amount
    tx_neg_amount = {
        "account_id": account_id,
        "symbol": "AAPL",
        "type": "BUY",
        "amount": -1,
        "price_per_unit": 100,
        "fees": 0,
        "executed_at": "2023-01-01T12:00:00"
    }
    r = client.post("/stocks/transactions", json=tx_neg_amount)
    assert r.status_code == 422

    # Negative price
    tx_neg_price = {
        "account_id": account_id,
        "symbol": "AAPL",
        "type": "BUY",
        "amount": 1,
        "price_per_unit": -100,
        "fees": 0,
        "executed_at": "2023-01-01T12:00:00"
    }
    r = client.post("/stocks/transactions", json=tx_neg_price)
    assert r.status_code == 422

    # Negative fees
    tx_neg_fees = {
        "account_id": account_id,
        "symbol": "AAPL",
        "type": "BUY",
        "amount": 1,
        "price_per_unit": 100,
        "fees": -5,
        "executed_at": "2023-01-01T12:00:00"
    }
    r = client.post("/stocks/transactions", json=tx_neg_fees)
    assert r.status_code == 422
