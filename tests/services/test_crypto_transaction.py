import pytest
from unittest.mock import patch, MagicMock
from decimal import Decimal
from datetime import datetime
from sqlmodel import Session

from services.crypto_transaction import (
    create_crypto_transaction,
    get_crypto_transaction,
    update_crypto_transaction,
    delete_crypto_transaction,
    get_account_transactions,
    get_crypto_account_summary,
)
from dtos.crypto import CryptoTransactionCreate, CryptoTransactionUpdate
from models.enums import CryptoTransactionType
from models.crypto import CryptoAccount, CryptoTransaction
from services.encryption import hash_index, encrypt_data


def test_create_crypto_transaction(session: Session, master_key: str):
    data = CryptoTransactionCreate(
        account_id="acc_crypto",
        ticker="BTC",
        type=CryptoTransactionType.BUY,
        amount=Decimal("0.5"),
        price_per_unit=Decimal("30000.0"),
        fees=Decimal("10.0"),
        executed_at=datetime(2023, 1, 1, 12, 0, 0),
        notes="First buy",
        tx_hash="0x123",
        fees_ticker="EUR"
    )

    resp = create_crypto_transaction(session, data, master_key)

    assert resp.ticker == "BTC"
    assert resp.type == "BUY"
    assert resp.amount == Decimal("0.5")
    assert resp.price_per_unit == Decimal("30000.0")
    assert resp.fees == Decimal("10.0")
    assert resp.total_cost == (Decimal("0.5") * Decimal("30000.0")) + Decimal("10.0")
    assert resp.executed_at == datetime(2023, 1, 1, 12, 0, 0)
    
    tx_db = session.get(CryptoTransaction, resp.id)
    assert tx_db is not None
    assert tx_db.ticker_enc != "BTC"
    assert tx_db.account_id_bidx == hash_index("acc_crypto", master_key)


def test_get_crypto_transaction(session: Session, master_key: str):
    data = CryptoTransactionCreate(
        account_id="acc_crypto",
        ticker="ETH",
        type=CryptoTransactionType.BUY,
        amount=Decimal("2"),
        price_per_unit=Decimal("2000"),
        fees=Decimal("5"),
        executed_at=datetime.now()
    )
    created = create_crypto_transaction(session, data, master_key)

    fetched = get_crypto_transaction(session, created.id, master_key)
    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.ticker == "ETH"

    assert get_crypto_transaction(session, "non_existent", master_key) is None
    
    # Test Bad Date Format coverage
    tx_db = session.get(CryptoTransaction, created.id)
    tx_db.executed_at_enc = encrypt_data("BAD DATE", master_key)
    session.add(tx_db)
    session.commit()
    fetched_bad = get_crypto_transaction(session, created.id, master_key)
    assert fetched_bad.executed_at == tx_db.created_at


def test_update_crypto_transaction(session: Session, master_key: str):
    data = CryptoTransactionCreate(
        account_id="acc_crypto",
        ticker="SOL",
        type=CryptoTransactionType.BUY,
        amount=Decimal("10"),
        price_per_unit=Decimal("20"),
        fees=Decimal("0.1"),
        executed_at=datetime(2023, 1, 1),
        notes="Old Note"
    )
    created = create_crypto_transaction(session, data, master_key)
    tx_db = session.get(CryptoTransaction, created.id)

    new_date = datetime(2023, 2, 2, 12, 0, 0)
    update_data = CryptoTransactionUpdate(
        ticker="SOLO",
        type=CryptoTransactionType.SELL,
        amount=Decimal("5"),
        price_per_unit=Decimal("25"),
        fees=Decimal("0.2"),
        fees_ticker="SOL",
        executed_at=new_date,
        notes="New Note",
        tx_hash="0xABC"
    )
    updated = update_crypto_transaction(session, tx_db, update_data, master_key)

    assert updated.ticker == "SOLO"
    assert updated.type == "SELL"
    assert updated.amount == Decimal("5")
    assert updated.price_per_unit == Decimal("25")
    assert updated.executed_at == new_date
    
    session.refresh(tx_db)
    from services.encryption import decrypt_data
    assert decrypt_data(tx_db.notes_enc, master_key) == "New Note"
    assert decrypt_data(tx_db.tx_hash_enc, master_key) == "0xABC"


def test_delete_crypto_transaction(session: Session, master_key: str):
    data = CryptoTransactionCreate(
        account_id="acc_crypto",
        ticker="ADA",
        type=CryptoTransactionType.BUY,
        amount=Decimal("100"),
        price_per_unit=Decimal("0.5"),
        fees=Decimal("0.1"),
        executed_at=datetime.now()
    )
    created = create_crypto_transaction(session, data, master_key)

    assert delete_crypto_transaction(session, created.id) is True
    assert session.get(CryptoTransaction, created.id) is None

    assert delete_crypto_transaction(session, "non_existent") is False


def test_get_account_transactions(session: Session, master_key: str):
    acc1 = "acc_c1"
    create_crypto_transaction(session, CryptoTransactionCreate(
        account_id=acc1, ticker="A", type=CryptoTransactionType.BUY, amount=Decimal(1), price_per_unit=Decimal(10), fees=Decimal(0), executed_at=datetime.now()
    ), master_key)
    create_crypto_transaction(session, CryptoTransactionCreate(
        account_id="acc_c2", ticker="B", type=CryptoTransactionType.BUY, amount=Decimal(1), price_per_unit=Decimal(10), fees=Decimal(0), executed_at=datetime.now()
    ), master_key)

    txs = get_account_transactions(session, acc1, master_key)
    assert len(txs) == 1
    assert txs[0].ticker == "A"


@patch("services.crypto_transaction.get_market_info")
@patch("services.crypto_transaction.get_market_price")
def test_get_crypto_account_summary(mock_price, mock_market, session: Session, master_key: str):
    # Mock Market Data
    mock_market.side_effect = lambda s, ticker: {
        "BTC": ("Bitcoin", Decimal("40000.0")),
        "ETH": ("Ethereum", Decimal("3000.0")),
    }.get(ticker, ("Unknown", Decimal("0")))
    
    # Mock Fee Price (e.g. paying fees in ETH for ERC20 or just ETH fees)
    mock_price.side_effect = lambda s, ticker: Decimal("3000.0") if ticker == "ETH" else Decimal("1.0")

    # Create Account
    account = CryptoAccount(
        uuid="acc_main_crypto",
        user_uuid_bidx=hash_index("user_1", master_key),
        name_enc=encrypt_data("My Wallet", master_key)
    )
    session.add(account)
    session.commit()

    # 1. Buy BTC: 1 @ 30000. Fees 10 EUR.
    create_crypto_transaction(session, CryptoTransactionCreate(
        account_id="acc_main_crypto", ticker="BTC", type=CryptoTransactionType.BUY, amount=Decimal("1"), price_per_unit=Decimal("30000"), fees=Decimal("10"), fees_ticker="EUR", executed_at=datetime(2023, 1, 1)
    ), master_key)

    # 2. Buy ETH: 10 @ 2000. Fees 0.01 ETH.
    # Cost: 20000. Fees in EUR: 0.01 * 3000 = 30 EUR. Total Cost basis logic in code: (Amount*Price) + Fees(EUR).
    # Code: fees_in_eur = fees * fees_price
    create_crypto_transaction(session, CryptoTransactionCreate(
        account_id="acc_main_crypto", ticker="ETH", type=CryptoTransactionType.BUY, amount=Decimal("10"), price_per_unit=Decimal("2000"), fees=Decimal("0.01"), fees_ticker="ETH", executed_at=datetime(2023, 1, 2)
    ), master_key)

    # 3. Sell BTC Half: 0.5 @ 35000.
    # Cost basis reduction: 50%.
    create_crypto_transaction(session, CryptoTransactionCreate(
        account_id="acc_main_crypto", ticker="BTC", type=CryptoTransactionType.SELL, amount=Decimal("0.5"), price_per_unit=Decimal("35000"), fees=Decimal("10"), fees_ticker="EUR", executed_at=datetime(2023, 1, 3)
    ), master_key)

    summary = get_crypto_account_summary(session, account, master_key)

    # Verify BTC
    pos_btc = next(p for p in summary.positions if p.ticker == "BTC")
    assert pos_btc.total_amount == Decimal("0.5")
    # Init Cost: 30000 + 10 = 30010.
    # Sell 50%: Remaining Cost = 15005.
    assert pos_btc.total_invested == Decimal("15005")
    # Current Value: 0.5 * 40000 = 20000
    assert pos_btc.current_value == Decimal("20000")
    # P&L: 20000 - 15005 = 4995
    assert pos_btc.profit_loss == Decimal("4995")

    # Verify ETH
    pos_eth = next(p for p in summary.positions if p.ticker == "ETH")
    assert pos_eth.total_amount == Decimal("10")
    # Cost: (10*2000) + (0.01*3000) = 20000 + 30 = 20030
    assert pos_eth.total_invested == Decimal("20030")
    # Current Value: 10 * 3000 = 30000
    assert pos_eth.current_value == Decimal("30000")
    
    # 4. Sell more than owned (Safety check coverage)
    # Owned ETH: 10. Sell 15.
    create_crypto_transaction(session, CryptoTransactionCreate(
        account_id="acc_main_crypto", ticker="ETH", type=CryptoTransactionType.SELL, amount=Decimal("15"), price_per_unit=Decimal("3000"), fees=Decimal("0"), executed_at=datetime(2023, 1, 4)
    ), master_key)
    
    summary_safety = get_crypto_account_summary(session, account, master_key)
    
    # Position should be closed (filtered out because amount <= 0)
    pos_eth_safety = next((p for p in summary_safety.positions if p.ticker == "ETH"), None)
    assert pos_eth_safety is None


@patch("services.crypto_transaction.get_market_price")
def test_get_crypto_transaction_fees_price_missing(mock_price, session: Session, master_key: str):
    """Test retrieving a transaction where fees are in crypto but market price is missing."""
    mock_price.return_value = None # Price missing
    
    data = CryptoTransactionCreate(
        account_id="acc_fees_test",
        ticker="BTC",
        type=CryptoTransactionType.BUY,
        amount=Decimal("1"),
        price_per_unit=Decimal("100"),
        fees=Decimal("1"),
        fees_ticker="SOL", # Non-EUR fees
        executed_at=datetime.now()
    )
    created = create_crypto_transaction(session, data, master_key)
    
    # fees_in_eur should fallback to raw fees (1) because price is None
    assert created.fees == Decimal("1")
    assert created.total_cost == (Decimal("1") * Decimal("100")) + Decimal("1")


def test_get_crypto_account_summary_empty(session: Session, master_key: str):
    """Test summary for an account with no transactions."""
    account = CryptoAccount(
        uuid="acc_empty",
        user_uuid_bidx=hash_index("u1", master_key),
        name_enc=encrypt_data("Empty", master_key)
    )
    session.add(account)
    session.commit()
    
    summary = get_crypto_account_summary(session, account, master_key)
    
    assert summary.total_invested == Decimal("0")
    assert summary.total_fees == Decimal("0")
    assert summary.current_value is None
    assert summary.profit_loss is None
    assert len(summary.positions) == 0