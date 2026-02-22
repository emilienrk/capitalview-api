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


@patch("routes.dashboard.get_exchange_rate")
@patch("services.stock_transaction.get_stock_info")
@patch("services.crypto_transaction.get_crypto_info")
def test_dashboard_portfolio(mock_crypto, mock_stock, mock_rate, session, master_key):
    mock_stock.return_value = ("Apple Inc.", Decimal("200"))
    mock_crypto.return_value = ("Bitcoin", Decimal("50000"))
    mock_rate.return_value = Decimal("0.90")

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
        "symbol": "AAPL",
        "isin": "US0378331005",
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
        "symbol": "BTC",
        "type": "BUY",
        "amount": "1",
        "price_per_unit": "30000",
        "fees": "10",
        "fees_symbol": "USD",
        "executed_at": "2023-01-01T12:00:00"
    }, headers=headers)

    r2 = client.get("/dashboard/portfolio", headers=headers)
    assert r2.status_code == 200
    summary = r2.json()
    assert Decimal(str(summary["total_invested"])) > 0

    # Verify crypto account was converted to EUR
    crypto_acc = next(a for a in summary["accounts"] if a["account_type"] == "CRYPTO")
    assert crypto_acc["currency"] == "EUR"
    # BTC invested = 30000 * 1 + 10 = 30010 USD → 30010 * 0.90 = 27009 EUR
    assert Decimal(str(crypto_acc["total_invested"])) == Decimal("27009")

    stock_acc = next(a for a in summary["accounts"] if a["account_type"] != "CRYPTO")
    assert stock_acc["currency"] == "EUR"
    # Stock invested stays in EUR = 100
    assert Decimal(str(stock_acc["total_invested"])) == Decimal("100")

    # Verify the exchange rate was called for USD→EUR
    mock_rate.assert_called_with("USD", "EUR")


@patch("routes.dashboard.get_exchange_rate")
@patch("routes.dashboard.get_user_bank_accounts")
@patch("routes.dashboard.get_user_assets")
@patch("services.stock_transaction.get_stock_info")
@patch("services.crypto_transaction.get_crypto_info")
def test_dashboard_statistics(
    mock_crypto, mock_stock, mock_assets, mock_bank, mock_rate, session, master_key
):
    from dtos import BankSummaryResponse
    from dtos.asset import AssetSummaryResponse

    mock_stock.return_value = ("Apple Inc.", Decimal("200"))
    mock_crypto.return_value = ("Bitcoin", Decimal("50000"))
    mock_rate.return_value = Decimal("0.90")

    mock_bank.return_value = BankSummaryResponse(total_balance=Decimal("5000"), accounts=[])
    mock_assets.return_value = AssetSummaryResponse(
        total_estimated_value=Decimal("10000"),
        total_purchase_price=Decimal("8000"),
        total_profit_loss=Decimal("2000"),
        asset_count=1,
        categories=[],
        assets=[],
    )

    client = TestClient(app)

    payload = {"username": "statuser", "email": "stat@example.com", "password": "StrongStat1!"}
    r = client.post("/auth/register", json=payload)
    assert r.status_code == 201
    token = r.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Create stock account + transaction
    acc = client.post("/stocks/accounts", json={"name": "Stat Stock", "account_type": "PEA"}, headers=headers)
    assert acc.status_code == 201
    acc_id = acc.json()["id"]
    client.post("/stocks/transactions", json={
        "account_id": acc_id,
        "symbol": "AAPL",
        "isin": "US0378331005",
        "type": "BUY",
        "amount": "2",
        "price_per_unit": "150",
        "fees": "0",
        "executed_at": "2024-01-01T12:00:00"
    }, headers=headers)

    # Create crypto account + transaction
    cacc = client.post("/crypto/accounts", json={"name": "Stat Crypto"}, headers=headers)
    assert cacc.status_code == 201
    cacc_id = cacc.json()["id"]
    client.post("/crypto/transactions", json={
        "account_id": cacc_id,
        "symbol": "BTC",
        "type": "BUY",
        "amount": "0.5",
        "price_per_unit": "40000",
        "fees": "5",
        "fees_symbol": "USD",
        "executed_at": "2024-01-01T12:00:00"
    }, headers=headers)

    r2 = client.get("/dashboard/statistics", headers=headers)
    assert r2.status_code == 200
    data = r2.json()

    # Check structure
    assert "distribution" in data
    assert "wealth" in data

    dist = data["distribution"]
    assert "stock_invested" in dist
    assert "crypto_invested" in dist
    assert "stock_percentage" in dist
    assert "crypto_percentage" in dist

    wealth = data["wealth"]
    assert Decimal(str(wealth["cash"])) == Decimal("5000")
    assert Decimal(str(wealth["assets"])) == Decimal("10000")
    assert Decimal(str(wealth["total_wealth"])) > 0

    # Percentages should sum to ~100
    if wealth["cash_percentage"] is not None:
        total_pct = (
            Decimal(str(wealth["cash_percentage"]))
            + Decimal(str(wealth["investments_percentage"]))
            + Decimal(str(wealth["assets_percentage"]))
        )
        assert abs(total_pct - 100) < 1
