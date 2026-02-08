import pytest
from unittest.mock import patch, MagicMock
from decimal import Decimal
from datetime import datetime
from sqlmodel import Session, select

from services.stock_transaction import (
    create_stock_transaction,
    get_stock_transaction,
    update_stock_transaction,
    delete_stock_transaction,
    get_account_transactions,
    get_stock_account_summary,
)
from dtos.stock import StockTransactionCreate, StockTransactionUpdate
from dtos.stock import StockTransactionCreate, StockTransactionUpdate
from models.enums import StockTransactionType
from models.stock import StockAccount, StockTransaction
from services.encryption import hash_index, encrypt_data


def test_create_stock_transaction(session: Session, master_key: str):
    data = StockTransactionCreate(
        account_id="acc_123",
        ticker="AAPL",
        type=StockTransactionType.BUY,
        amount=Decimal("10.5"),
        price_per_unit=Decimal("150.0"),
        fees=Decimal("2.5"),
        executed_at=datetime(2023, 1, 1, 12, 0, 0),
        notes="First buy",
        exchange="NASDAQ"
    )

    resp = create_stock_transaction(session, data, master_key)

    assert resp.ticker == "AAPL"
    assert resp.type == "BUY"
    assert resp.amount == Decimal("10.5")
    assert resp.price_per_unit == Decimal("150.0")
    assert resp.total_cost == (Decimal("10.5") * Decimal("150.0")) + Decimal("2.5")
    assert resp.executed_at == datetime(2023, 1, 1, 12, 0, 0)
    
    # Verify DB content is encrypted
    tx_db = session.get(StockTransaction, resp.id)
    assert tx_db is not None
    assert tx_db.ticker_enc != "AAPL"
    assert tx_db.amount_enc != "10.5"
    assert tx_db.account_id_bidx == hash_index("acc_123", master_key)


def test_get_stock_transaction(session: Session, master_key: str):
    # Setup
    data = StockTransactionCreate(
        account_id="acc_123",
        ticker="MSFT",
        type=StockTransactionType.BUY,
        amount=Decimal("5"),
        price_per_unit=Decimal("200"),
        fees=Decimal("1"),
        executed_at=datetime.now()
    )
    created = create_stock_transaction(session, data, master_key)

    # Test
    fetched = get_stock_transaction(session, created.id, master_key)
    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.ticker == "MSFT"

    # Test Not Found
    assert get_stock_transaction(session, "non_existent", master_key) is None

    # Test Bad Date Format (for coverage)
    # Manually corrupt the date
    tx_db = session.get(StockTransaction, created.id)
    tx_db.executed_at_enc = encrypt_data("NOT A DATE", master_key)
    session.add(tx_db)
    session.commit()
    
    fetched_bad_date = get_stock_transaction(session, created.id, master_key)
    # Should fallback to created_at
    assert fetched_bad_date.executed_at == tx_db.created_at


def test_update_stock_transaction(session: Session, master_key: str):
    # Setup
    data = StockTransactionCreate(
        account_id="acc_123",
        ticker="GOOGL",
        type=StockTransactionType.BUY,
        amount=Decimal("10"),
        price_per_unit=Decimal("100"),
        fees=Decimal("0"),
        executed_at=datetime(2023, 1, 1),
        notes="Old Note"
    )
    created = create_stock_transaction(session, data, master_key)
    tx_db = session.get(StockTransaction, created.id)

    # Update ALL fields
    new_date = datetime(2023, 2, 2, 12, 0, 0)
    update_data = StockTransactionUpdate(
        ticker="GOOG",
        exchange="NASDAQ",
        type=StockTransactionType.SELL,
        amount=Decimal("20"),
        price_per_unit=Decimal("105"),
        fees=Decimal("1"),
        executed_at=new_date,
        notes="Updated note"
    )
    updated = update_stock_transaction(session, tx_db, update_data, master_key)

    assert updated.ticker == "GOOG"
    assert updated.type == "SELL"
    assert updated.amount == Decimal("20")
    assert updated.price_per_unit == Decimal("105")
    assert updated.fees == Decimal("1")
    assert updated.executed_at == new_date
    # Note: exchange is not in TransactionResponse usually, let's check DB
    session.refresh(tx_db)
    from services.encryption import decrypt_data
    assert decrypt_data(tx_db.exchange_enc, master_key) == "NASDAQ"
    assert decrypt_data(tx_db.notes_enc, master_key) == "Updated note"


def test_delete_stock_transaction(session: Session, master_key: str):
    # Setup
    data = StockTransactionCreate(
        account_id="acc_123",
        ticker="TSLA",
        type=StockTransactionType.BUY,
        amount=Decimal("1"),
        price_per_unit=Decimal("500"),
        fees=Decimal("1"),
        executed_at=datetime.now()
    )
    created = create_stock_transaction(session, data, master_key)

    # Delete
    assert delete_stock_transaction(session, created.id) is True
    assert session.get(StockTransaction, created.id) is None

    # Delete non-existent
    assert delete_stock_transaction(session, "non_existent") is False


def test_get_account_transactions(session: Session, master_key: str):
    acc1 = "acc_1"
    acc2 = "acc_2"

    # Create 2 for acc1, 1 for acc2
    create_stock_transaction(session, StockTransactionCreate(
        account_id=acc1, ticker="A", type=StockTransactionType.BUY, amount=Decimal(1), price_per_unit=Decimal(10), fees=Decimal(0), executed_at=datetime.now()
    ), master_key)
    create_stock_transaction(session, StockTransactionCreate(
        account_id=acc1, ticker="B", type=StockTransactionType.BUY, amount=Decimal(1), price_per_unit=Decimal(10), fees=Decimal(0), executed_at=datetime.now()
    ), master_key)
    create_stock_transaction(session, StockTransactionCreate(
        account_id=acc2, ticker="C", type=StockTransactionType.BUY, amount=Decimal(1), price_per_unit=Decimal(10), fees=Decimal(0), executed_at=datetime.now()
    ), master_key)

    txs_1 = get_account_transactions(session, acc1, master_key)
    assert len(txs_1) == 2
    tickers = {t.ticker for t in txs_1}
    assert tickers == {"A", "B"}

    txs_2 = get_account_transactions(session, acc2, master_key)
    assert len(txs_2) == 1
    assert txs_2[0].ticker == "C"


@patch("services.stock_transaction.get_market_info")
def test_get_stock_account_summary(mock_market, session: Session, master_key: str):
    mock_market.side_effect = lambda s, ticker: {
        "AAPL": ("Apple Inc.", Decimal("180.0")),
        "MSFT": ("Microsoft", Decimal("300.0")),
        "SOLD": ("Sold Stock", Decimal("10.0")),
    }.get(ticker, ("Unknown", Decimal("0")))

    account = StockAccount(
        uuid="acc_main",
        user_uuid_bidx=hash_index("user_1", master_key),
        name_enc=encrypt_data("My PEA", master_key),
        account_type_enc=encrypt_data("PEA", master_key),
    )
    session.add(account)
    session.commit()

    # Create Transactions
    create_stock_transaction(session, StockTransactionCreate(
        account_id="acc_main", ticker="AAPL", type=StockTransactionType.BUY, amount=Decimal("10"), price_per_unit=Decimal("150"), fees=Decimal("5"), executed_at=datetime(2023, 1, 1)
    ), master_key)

    create_stock_transaction(session, StockTransactionCreate(
        account_id="acc_main", ticker="AAPL", type=StockTransactionType.BUY, amount=Decimal("5"), price_per_unit=Decimal("160"), fees=Decimal("2"), executed_at=datetime(2023, 1, 2)
    ), master_key)

    create_stock_transaction(session, StockTransactionCreate(
        account_id="acc_main", ticker="MSFT", type=StockTransactionType.BUY, amount=Decimal("10"), price_per_unit=Decimal("250"), fees=Decimal("5"), executed_at=datetime(2023, 1, 3)
    ), master_key)

    create_stock_transaction(session, StockTransactionCreate(
        account_id="acc_main", ticker="MSFT", type=StockTransactionType.SELL, amount=Decimal("5"), price_per_unit=Decimal("280"), fees=Decimal("5"), executed_at=datetime(2023, 1, 4)
    ), master_key)

    create_stock_transaction(session, StockTransactionCreate(
        account_id="acc_main", ticker="SOLD", type=StockTransactionType.BUY, amount=Decimal("10"), price_per_unit=Decimal("10"), fees=Decimal("1"), executed_at=datetime(2023, 1, 5)
    ), master_key)
    create_stock_transaction(session, StockTransactionCreate(
        account_id="acc_main", ticker="SOLD", type=StockTransactionType.SELL, amount=Decimal("10"), price_per_unit=Decimal("12"), fees=Decimal("1"), executed_at=datetime(2023, 1, 6)
    ), master_key)

    # Execute
    summary = get_stock_account_summary(session, account, master_key)

    assert summary.account_name == "My PEA"
    

    pos_aapl = next(p for p in summary.positions if p.ticker == "AAPL")
    assert pos_aapl.total_amount == Decimal("15")
    assert pos_aapl.total_invested == Decimal("2307")
    assert pos_aapl.average_buy_price == round(Decimal("2307") / 15, 4)
    assert pos_aapl.current_value == Decimal("2700")
    assert pos_aapl.profit_loss == Decimal("393")

    pos_msft = next(p for p in summary.positions if p.ticker == "MSFT")
    assert pos_msft.total_amount == Decimal("5")
    assert pos_msft.total_invested == Decimal("1252.5")
    assert pos_msft.current_value == Decimal("1500")

    pos_sold = next((p for p in summary.positions if p.ticker == "SOLD"), None)
    assert pos_sold is None

    assert summary.total_invested == Decimal("3559.5")
