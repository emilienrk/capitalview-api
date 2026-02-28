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


@patch("routes.crypto.get_effective_usd_eur_rate", return_value=Decimal("1"))
@patch("services.crypto_transaction.get_crypto_info")
def test_crypto_summary(mock_get_info, _mock_rate, session, master_key):
    # Decorators are applied bottom-up → mock_get_info  = get_crypto_info,
    #                                     _mock_rate     = get_effective_usd_eur_rate
    mock_get_info.return_value = ("Bitcoin", Decimal("40000"))

    client = TestClient(app)
    resp = client.post("/crypto/accounts", json={"name": "Summary Wallet"})
    assert resp.status_code == 201
    account_id = resp.json()["id"]

    # Wire in capital then buy — correct model requires an external EUR deposit
    client.post("/crypto/transactions/composite", json={
        "account_id": account_id,
        "symbol": "EUR",
        "type": "FIAT_DEPOSIT",
        "amount": "30000",
        "executed_at": "2023-01-01T11:00:00",
    })
    client.post("/crypto/transactions/composite", json={
        "account_id": account_id,
        "symbol": "BTC",
        "type": "BUY",
        "amount": "1",
        "eur_amount": "30000",
        "quote_symbol": "EUR",
        "quote_amount": "30000",
        "executed_at": "2023-01-01T12:00:00",
    })

    r = client.get(f"/crypto/accounts/{account_id}")
    assert r.status_code == 200
    summary = r.json()

    pos = next(p for p in summary["positions"] if p["symbol"] == "BTC")
    assert Decimal(str(pos["total_amount"])) == Decimal("1")
    assert Decimal(str(pos["current_price"])) == Decimal("40000")
    # BTC current value = 40 000, wired in = 30 000 → P/L = +10 000
    assert Decimal(str(summary["total_invested"])) == Decimal("30000")
    assert Decimal(str(summary["profit_loss"])) == Decimal("10000")


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


def test_bulk_import_with_group_uuid(session, master_key):
    """Bulk import with explicit group_uuid links atomic rows (BUY + SPEND EUR).
    PRU should be computed via the shared group_uuid, not the fallback price."""
    client = TestClient(app)
    racc = client.post("/crypto/accounts", json={"name": "GroupedBulkWallet"})
    assert racc.status_code == 201
    account_id = racc.json()["id"]

    group = "test-group-uuid-1234"
    bulk = {
        "account_id": account_id,
        "transactions": [
            # Atomic BUY row: price=0, cost carried by SPEND EUR via group_uuid
            {
                "symbol": "BTC", "type": "BUY", "amount": "0.1",
                "price_per_unit": "0",
                "executed_at": "2024-01-01T12:00:00",
                "group_uuid": group,
            },
            # Atomic SPEND EUR row: carries the EUR cost
            {
                "symbol": "EUR", "type": "SPEND", "amount": "3000",
                "price_per_unit": "1",
                "executed_at": "2024-01-01T12:00:00",
                "group_uuid": group,
            },
        ]
    }
    rbulk = client.post("/crypto/transactions/bulk", json=bulk)
    assert rbulk.status_code == 201
    body = rbulk.json()
    assert body["imported_count"] == 2
    # group_uuid must be propagated in the response
    assert all(tx["group_uuid"] == group for tx in body["transactions"])

    # Verify PRU is computed correctly via the shared group (3000 / 0.1 = 30 000)
    summary = client.get(f"/crypto/accounts/{account_id}").json()
    btc = next(p for p in summary["positions"] if p["symbol"] == "BTC")
    assert Decimal(str(btc["total_amount"])) == Decimal("0.1")
    assert Decimal(str(btc["total_invested"])) == Decimal("3000.00")
    assert Decimal(str(btc["average_buy_price"])) == Decimal("30000.0000")


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


@patch("routes.crypto.get_effective_usd_eur_rate", return_value=Decimal("1"))
@patch("services.crypto_transaction.get_crypto_info")
def test_bulk_composite_import_eur_buy(mock_info, _mock_rate, session, master_key):
    """
    Bulk-composite: import one BUY BTC for EUR from CSV.
    Creates BUY BTC + SPEND EUR → 2 atomic rows, PRU = 3000/0.1 = 30 000.
    """
    mock_info.return_value = ("Bitcoin", Decimal("35000"))

    client = TestClient(app)
    acc = client.post("/crypto/accounts", json={"name": "CSV Wallet"}).json()
    account_id = acc["id"]

    payload = {
        "account_id": account_id,
        "transactions": [
            {
                "symbol": "BTC",
                "type": "BUY",
                "amount": "0.1",
                "eur_amount": "3000",
                "quote_symbol": "EUR",
                "quote_amount": "3000",
                "executed_at": "2024-01-01T12:00:00",
            }
        ],
    }
    r = client.post("/crypto/transactions/bulk-composite", json=payload)
    assert r.status_code == 201
    body = r.json()
    assert body["groups_count"] == 1
    assert body["imported_count"] == 2  # BUY BTC + SPEND EUR

    summary = client.get(f"/crypto/accounts/{account_id}").json()
    btc = next(p for p in summary["positions"] if p["symbol"] == "BTC")
    assert Decimal(str(btc["total_amount"])) == Decimal("0.1")
    assert Decimal(str(btc["total_invested"])) == Decimal("3000.00")
    assert Decimal(str(btc["average_buy_price"])) == Decimal("30000.0000")


@patch("routes.crypto.get_effective_usd_eur_rate", return_value=Decimal("1"))
@patch("services.crypto_transaction.get_crypto_info")
def test_bulk_composite_import_crypto_swap_with_eur_anchor(mock_info, _mock_rate, session, master_key):
    """
    Bulk-composite: swap USDC→BTC with eur_amount.
    Creates BUY BTC + SPEND USDC + FIAT_ANCHOR → 3 atomic rows.
    PRU = 2760/0.1 = 27 600.
    """
    mock_info.return_value = ("Bitcoin", Decimal("35000"))

    client = TestClient(app)
    acc = client.post("/crypto/accounts", json={"name": "CSV Swap Wallet"}).json()
    account_id = acc["id"]

    payload = {
        "account_id": account_id,
        "transactions": [
            {
                "symbol": "BTC",
                "type": "BUY",
                "amount": "0.1",
                "eur_amount": "2760",
                "quote_symbol": "USDC",
                "quote_amount": "3000",
                "executed_at": "2024-01-01T12:00:00",
            }
        ],
    }
    r = client.post("/crypto/transactions/bulk-composite", json=payload)
    assert r.status_code == 201
    body = r.json()
    assert body["groups_count"] == 1
    assert body["imported_count"] == 3  # BUY + SPEND + FIAT_ANCHOR

    summary = client.get(f"/crypto/accounts/{account_id}").json()
    btc = next(p for p in summary["positions"] if p["symbol"] == "BTC")
    assert Decimal(str(btc["total_invested"])) == Decimal("2760.00")
    assert Decimal(str(btc["average_buy_price"])) == Decimal("27600.0000")


@patch("routes.crypto.get_effective_usd_eur_rate", return_value=Decimal("1"))
@patch("services.crypto_transaction.get_crypto_info")
def test_bulk_composite_import_reward_and_crypto_deposit(mock_info, _mock_rate, session, master_key):
    """
    Bulk-composite: REWARD (cost=0) + CRYPTO_DEPOSIT with eur_amount.
    - REWARD → 1 row, total_invested = 0
    - CRYPTO_DEPOSIT → 2 rows (FIAT_ANCHOR + BUY), cost = 15000
    """
    mock_info.side_effect = lambda s, symbol: {
        "ETH": ("Ethereum", Decimal("3000")),
        "BTC": ("Bitcoin", Decimal("35000")),
    }.get(symbol, ("?", Decimal("0")))

    client = TestClient(app)
    acc = client.post("/crypto/accounts", json={"name": "CSV Multi Wallet"}).json()
    account_id = acc["id"]

    payload = {
        "account_id": account_id,
        "transactions": [
            {
                "symbol": "ETH",
                "type": "REWARD",
                "amount": "2.5",
                "eur_amount": "0",
                "executed_at": "2024-01-01T09:00:00",
            },
            {
                "symbol": "BTC",
                "type": "CRYPTO_DEPOSIT",
                "amount": "0.5",
                "eur_amount": "15000",
                "executed_at": "2024-01-02T12:00:00",
            },
        ],
    }
    r = client.post("/crypto/transactions/bulk-composite", json=payload)
    assert r.status_code == 201
    body = r.json()
    assert body["groups_count"] == 2
    assert body["imported_count"] == 3  # 1 REWARD + 2 (FIAT_ANCHOR + BUY)

    summary = client.get(f"/crypto/accounts/{account_id}").json()

    eth = next(p for p in summary["positions"] if p["symbol"] == "ETH")
    assert Decimal(str(eth["total_amount"])) == Decimal("2.5")
    assert Decimal(str(eth["total_invested"])) == Decimal("0.00")  # REWARD: no cost

    btc = next(p for p in summary["positions"] if p["symbol"] == "BTC")
    assert Decimal(str(btc["total_amount"])) == Decimal("0.5")
    assert Decimal(str(btc["total_invested"])) == Decimal("15000.00")


def test_bulk_composite_with_fee_creates_extra_row(session, master_key):
    """
    BUY BTC with EUR + BNB fee (fee_included=True):
    BUY BTC + SPEND EUR + FEE BNB = 3 atomic rows.
    """
    client = TestClient(app)
    acc = client.post("/crypto/accounts", json={"name": "CSV Fee Wallet"}).json()
    account_id = acc["id"]

    payload = {
        "account_id": account_id,
        "transactions": [
            {
                "symbol": "BTC",
                "type": "BUY",
                "amount": "0.1",
                "eur_amount": "3000",
                "quote_symbol": "EUR",
                "quote_amount": "3000",
                "fee_symbol": "BNB",
                "fee_amount": "0.01",
                "fee_included": True,
                "executed_at": "2024-01-01T12:00:00",
            }
        ],
    }
    r = client.post("/crypto/transactions/bulk-composite", json=payload)
    assert r.status_code == 201
    assert r.json()["imported_count"] == 3  # BUY + SPEND EUR + FEE BNB


def test_bulk_composite_account_not_found(session, master_key):
    """bulk-composite returns 404 when account does not exist."""
    client = TestClient(app)
    payload = {
        "account_id": "nonexistent-account",
        "transactions": [
            {
                "symbol": "BTC",
                "type": "BUY",
                "amount": "0.1",
                "eur_amount": "3000",
                "quote_symbol": "EUR",
                "quote_amount": "3000",
                "executed_at": "2024-01-01T12:00:00",
            }
        ],
    }
    r = client.post("/crypto/transactions/bulk-composite", json=payload)
    assert r.status_code == 404


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
    data = r.json()
    assert "rows" in data
    rows = data["rows"]
    assert data["warning"] is None  # first-ever purchase — no deficit possible
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
    data = r.json()
    assert "rows" in data
    rows = data["rows"]
    # USDC balance goes negative on first purchase — warning is expected here
    assert len(rows) == 3  # BUY + SPEND + FIAT_ANCHOR
    types = {row["type"] for row in rows}
    assert types == {"BUY", "SPEND", "FIAT_ANCHOR"}
    # All rows share the same group_uuid
    assert len({row["group_uuid"] for row in rows}) == 1


@patch("routes.crypto.get_effective_usd_eur_rate", return_value=Decimal("1"))
@patch("services.crypto_transaction.get_crypto_info")
def test_account_pl_eur_cash_not_profit(mock_get_info, _mock_rate, session, master_key):
    """
    Core regression: the EUR cash sitting in an exchange account must NOT be
    counted as profit.

    Scenario
    --------
    1. User wires 10 000 € from bank → exchange  (FIAT_DEPOSIT)
    2. Buys 1 BTC at 8 000 €                      (BUY + SPEND EUR, same group)
    3. BTC current price = 7 000 €

    Expected account-level summary
    --------------------------------
    total_invested  = 10 000  (net EUR wired from outside)
    current_value   =  9 000  (7 000 BTC + 2 000 EUR cash)
    profit_loss     = -1 000  (not +2 000 from EUR cash)
    """
    mock_get_info.return_value = ("Bitcoin", Decimal("7000"))

    client = TestClient(app)
    acc = client.post("/crypto/accounts", json={"name": "PL Test Wallet"}).json()
    account_id = acc["id"]

    # 1. Wire 10 000 € into the exchange account
    r = client.post("/crypto/transactions/composite", json={
        "account_id": account_id,
        "symbol": "EUR",
        "type": "FIAT_DEPOSIT",
        "amount": "10000",
        "executed_at": "2024-01-01T10:00:00",
    })
    assert r.status_code == 201

    # 2. Buy 1 BTC with 8 000 €
    r = client.post("/crypto/transactions/composite", json={
        "account_id": account_id,
        "symbol": "BTC",
        "type": "BUY",
        "amount": "1",
        "eur_amount": "8000",
        "quote_symbol": "EUR",
        "quote_amount": "8000",
        "executed_at": "2024-01-02T10:00:00",
    })
    assert r.status_code == 201

    summary = client.get(f"/crypto/accounts/{account_id}").json()

    assert Decimal(str(summary["total_invested"])) == Decimal("10000"), (
        "total_invested must equal the EUR wired in, not the crypto cost_basis"
    )
    assert Decimal(str(summary["current_value"])) == Decimal("9000"), (
        "current_value must include both BTC (7 000) and EUR cash (2 000)"
    )
    assert Decimal(str(summary["profit_loss"])) == Decimal("-1000"), (
        "profit_loss = 9 000 - 10 000 = -1 000 (EUR cash must NOT be counted as profit)"
    )


@patch("routes.crypto.get_effective_usd_eur_rate", return_value=Decimal("1"))
@patch("services.crypto_transaction.get_crypto_info")
def test_account_pl_exit_does_not_inflate_invested(mock_get_info, _mock_rate, session, master_key):
    """
    Selling crypto for EUR (EXIT) moves value back to EUR cash but must NOT
    inflate total_invested (the EUR received is NOT a new bank wire).

    Scenario
    --------
    1. Wire 10 000 €                  FIAT_DEPOSIT  → net_invested = 10 000
    2. Buy 1 BTC at 8 000 €           BUY + SPEND   → net_invested = 10 000
    3. Sell 1 BTC at 7 000 € (EXIT)   SPEND + FIAT_DEPOSIT → net_invested must stay 10 000

    After sell: EUR cash = 2 000 + 7 000 = 9 000, no BTC.
    profit_loss = 9 000 - 10 000 = -1 000
    """
    mock_get_info.return_value = ("Bitcoin", Decimal("7000"))

    client = TestClient(app)
    acc = client.post("/crypto/accounts", json={"name": "EXIT PL Wallet"}).json()
    account_id = acc["id"]

    # 1. Wire 10 000 €
    assert client.post("/crypto/transactions/composite", json={
        "account_id": account_id,
        "symbol": "EUR",
        "type": "FIAT_DEPOSIT",
        "amount": "10000",
        "executed_at": "2024-01-01T10:00:00",
    }).status_code == 201

    # 2. Buy 1 BTC with 8 000 €
    assert client.post("/crypto/transactions/composite", json={
        "account_id": account_id,
        "symbol": "BTC",
        "type": "BUY",
        "amount": "1",
        "eur_amount": "8000",
        "quote_symbol": "EUR",
        "quote_amount": "8000",
        "executed_at": "2024-01-02T10:00:00",
    }).status_code == 201

    # 3. Sell 1 BTC at 7 000 € (EXIT)
    assert client.post("/crypto/transactions/composite", json={
        "account_id": account_id,
        "symbol": "BTC",
        "type": "EXIT",
        "amount": "1",
        "eur_amount": "7000",
        "executed_at": "2024-01-03T10:00:00",
    }).status_code == 201

    summary = client.get(f"/crypto/accounts/{account_id}").json()

    assert Decimal(str(summary["total_invested"])) == Decimal("10000"), (
        "Selling crypto (EXIT) must NOT increase total_invested — "
        "the received EUR is not a new bank wire"
    )
    # Only EUR remains: 2 000 + 7 000 = 9 000
    assert Decimal(str(summary["current_value"])) == Decimal("9000")
    assert Decimal(str(summary["profit_loss"])) == Decimal("-1000")


# ── Balance warning tests ────────────────────────────────────────────────────

def test_composite_no_warning_when_sufficient_balance(session, master_key):
    """No warning when the user has enough crypto before spending it."""
    client = TestClient(app)
    acc = client.post("/crypto/accounts", json={"name": "Balance OK Wallet"}).json()
    account_id = acc["id"]

    # First deposit 10 000 EUR
    client.post("/crypto/transactions/composite", json={
        "account_id": account_id,
        "symbol": "EUR",
        "type": "FIAT_DEPOSIT",
        "amount": "10000",
        "executed_at": "2024-01-01T10:00:00",
    })

    # Buy 1 BTC with 8 000 EUR (EUR balance stays positive: 2 000 remaining)
    r = client.post("/crypto/transactions/composite", json={
        "account_id": account_id,
        "symbol": "BTC",
        "type": "BUY",
        "amount": "1",
        "eur_amount": "8000",
        "quote_symbol": "EUR",
        "quote_amount": "8000",
        "executed_at": "2024-01-02T10:00:00",
    })
    assert r.status_code == 201
    data = r.json()
    # EUR is fiat — not checked as a crypto holding; no crypto debited
    assert data["warning"] is None


def test_composite_warning_when_insufficient_crypto(session, master_key):
    """Warning is set (but transaction persists) when TRANSFER exceeds holding."""
    client = TestClient(app)
    acc = client.post("/crypto/accounts", json={"name": "Overdraft Wallet"}).json()
    account_id = acc["id"]

    # Buy only 0.5 BTC
    client.post("/crypto/transactions/composite", json={
        "account_id": account_id,
        "symbol": "BTC",
        "type": "BUY",
        "amount": "0.5",
        "eur_amount": "15000",
        "executed_at": "2024-01-01T10:00:00",
    })

    # Attempt to TRANSFER 1 BTC (more than the 0.5 BTC held)
    r = client.post("/crypto/transactions/composite", json={
        "account_id": account_id,
        "symbol": "BTC",
        "type": "TRANSFER",
        "amount": "1",
        "executed_at": "2024-01-02T10:00:00",
    })
    assert r.status_code == 201  # non-blocking — transaction was created
    data = r.json()
    assert len(data["rows"]) >= 1  # rows were created
    assert data["warning"] is not None
    assert "BTC" in data["warning"]


def test_composite_warning_absent_when_balance_exact(session, master_key):
    """No warning when the user transfers exactly their entire holding."""
    client = TestClient(app)
    acc = client.post("/crypto/accounts", json={"name": "Exact Balance Wallet"}).json()
    account_id = acc["id"]

    # Buy exactly 1 ETH
    client.post("/crypto/transactions/composite", json={
        "account_id": account_id,
        "symbol": "ETH",
        "type": "BUY",
        "amount": "1",
        "eur_amount": "2000",
        "executed_at": "2024-01-01T10:00:00",
    })

    # Transfer exactly 1 ETH (balance becomes 0 — no warning)
    r = client.post("/crypto/transactions/composite", json={
        "account_id": account_id,
        "symbol": "ETH",
        "type": "TRANSFER",
        "amount": "1",
        "executed_at": "2024-01-02T10:00:00",
    })
    assert r.status_code == 201
    data = r.json()
    assert data["warning"] is None

