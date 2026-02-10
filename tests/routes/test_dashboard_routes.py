from decimal import Decimal
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from main import app


@pytest.fixture(autouse=True)
def _override_deps(session, master_key):
    def _get_session():
        return session

    app.dependency_overrides.clear()
    from database import get_session
    app.dependency_overrides[get_session] = _get_session
    yield
    app.dependency_overrides.clear()


@patch("services.stock_transaction.get_market_info")
@patch("services.crypto_transaction.get_market_info")
def test_dashboard_portfolio(mock_crypto, mock_stock, session, master_key):
    mock_stock.return_value = ("Apple Inc.", Decimal("200"))
    mock_crypto.return_value = ("Bitcoin", Decimal("40000"))

    client = TestClient(app)

    payload = {"username": "dashu", "email": "dash@example.com", "password": "StrongDash1!"}
    r = client.post("/auth/register", json=payload)
    assert r.status_code == 201
    token = r.json()["access_token"]

    headers = {"Authorization": f"Bearer {token}"}

    acc = client.post("/stocks/accounts", json={"name": "Dash Stock", "account_type": "CTO"}, headers=headers)
    assert acc.status_code == 201
    acc_id = acc.json()["id"]

    client.post("/stocks/transactions", json={
        "account_id": acc_id,
        "ticker": "AAPL",
        "type": "BUY",
        "amount": "1",
        "price_per_unit": "100",
        "fees": "0",
        "executed_at": "2023-01-01T12:00:00"
    }, headers=headers)

    cacc = client.post("/crypto/accounts", json={"name": "Dash Crypto"}, headers=headers)
    assert cacc.status_code == 201
    cacc_id = cacc.json()["id"]

    client.post("/crypto/transactions", json={
        "account_id": cacc_id,
        "ticker": "BTC",
        "type": "BUY",
        "amount": "1",
        "price_per_unit": "30000",
        "fees": "10",
        "fees_ticker": "EUR",
        "executed_at": "2023-01-01T12:00:00"
    }, headers=headers)

    r2 = client.get("/dashboard/portfolio", headers=headers)
    assert r2.status_code == 200
    summary = r2.json()
    assert Decimal(str(summary["total_invested"])) > 0
