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
from services.encryption import hash_index, encrypt_data, decrypt_data


def test_create_stock_transaction(session: Session, master_key: str):
    data = StockTransactionCreate(
        account_id="acc_123",
        symbol="AAPL",
        isin="ISIN_AAPL",
        type=StockTransactionType.BUY,
        amount=Decimal("10.5"),
        price_per_unit=Decimal("150.0"),
        fees=Decimal("2.5"),
        executed_at=datetime(2023, 1, 1, 12, 0, 0),
        notes="First buy",
        exchange="NASDAQ"
    )
    resp = create_stock_transaction(session, data, master_key)
    # Symbol comes from enrichment (MarketPrice created during transaction creation)
    assert resp.symbol == "AAPL"
    assert resp.exchange == "NASDAQ"
    assert resp.type == "BUY"
    assert resp.amount == Decimal("10.5")
    assert resp.price_per_unit == Decimal("150.0")
    assert resp.total_cost == (Decimal("10.5") * Decimal("150.0")) + Decimal("2.5")
    assert resp.executed_at == datetime(2023, 1, 1, 12, 0, 0)
    tx_db = session.get(StockTransaction, resp.id)
    assert tx_db is not None
    # Verify fields are GONE (or at least not used/set). 
    # Since we removed them from model, we can't access tx_db.symbol_enc.
    # But SqlAlchemy might still allow access if class definition has them (which it doesn't).
    assert not hasattr(tx_db, "symbol_enc")
    assert not hasattr(tx_db, "exchange_enc")
    assert not hasattr(tx_db, "name_enc")
    assert tx_db.account_id_bidx == hash_index("acc_123", master_key)


def test_get_stock_transaction(session: Session, master_key: str):
    data = StockTransactionCreate(
        account_id="acc_123",
        symbol="MSFT",
        isin="ISIN_MSFT",
        type=StockTransactionType.BUY,
        amount=Decimal("5"),
        price_per_unit=Decimal("200"),
        fees=Decimal("1"),
        executed_at=datetime.now()
    )
    created = create_stock_transaction(session, data, master_key)
    fetched = get_stock_transaction(session, created.id, master_key)
    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.symbol == "MSFT"
    assert get_stock_transaction(session, "non_existent", master_key) is None
    tx_db = session.get(StockTransaction, created.id)
    tx_db.executed_at_enc = encrypt_data("NOT A DATE", master_key)
    session.add(tx_db)
    session.commit()
    fetched_bad_date = get_stock_transaction(session, created.id, master_key)
    assert fetched_bad_date.executed_at == tx_db.created_at


def test_update_stock_transaction(session: Session, master_key: str):
    data = StockTransactionCreate(
        account_id="acc_123",
        symbol="GOOGL",
        isin="ISIN_GOOGL",
        type=StockTransactionType.BUY,
        amount=Decimal("10"),
        price_per_unit=Decimal("100"),
        fees=Decimal("0"),
        executed_at=datetime(2023, 1, 1),
        notes="Old Note"
    )
    created = create_stock_transaction(session, data, master_key)
    tx_db = session.get(StockTransaction, created.id)
    new_date = datetime(2023, 2, 2, 12, 0, 0)
    update_data = StockTransactionUpdate(
        symbol="GOOG", # This update is ignored for MarketPrice/Transaction storage, but logic checks?
        exchange="NASDAQ",
        type=StockTransactionType.SELL,
        amount=Decimal("20"),
        price_per_unit=Decimal("105"),
        fees=Decimal("1"),
        executed_at=new_date,
        notes="Updated note"
    )
    # Note: update_stock_transaction does NOT update MarketPrice symbol.
    # So returned symbol should still be "GOOGL" from initial creation.
    updated = update_stock_transaction(session, tx_db, update_data, master_key)
    
    assert updated.symbol == "GOOGL" 
    assert updated.type == "SELL"
    assert updated.amount == Decimal("20")
    assert updated.price_per_unit == Decimal("105")
    assert updated.fees == Decimal("1")
    assert updated.executed_at == new_date
    session.refresh(tx_db)
    
    # Verify exchange_enc is gone
    assert not hasattr(tx_db, "exchange_enc")
    assert decrypt_data(tx_db.notes_enc, master_key) == "Updated note"


def test_delete_stock_transaction(session: Session, master_key: str):
    data = StockTransactionCreate(
        account_id="acc_123",
        symbol="TSLA",
        isin="ISIN_TSLA",
        type=StockTransactionType.BUY,
        amount=Decimal("1"),
        price_per_unit=Decimal("500"),
        fees=Decimal("1"),
        executed_at=datetime.now()
    )
    created = create_stock_transaction(session, data, master_key)
    assert delete_stock_transaction(session, created.id) is True
    assert session.get(StockTransaction, created.id) is None
    assert delete_stock_transaction(session, "non_existent") is False


def test_get_account_transactions(session: Session, master_key: str):
    acc1 = "acc_1"
    acc2 = "acc_2"
    create_stock_transaction(session, StockTransactionCreate(
        account_id=acc1, symbol="A", isin="ISIN_A", type=StockTransactionType.BUY, amount=Decimal(1), price_per_unit=Decimal(10), fees=Decimal(0), executed_at=datetime.now()
    ), master_key)
    create_stock_transaction(session, StockTransactionCreate(
        account_id=acc1, symbol="B", isin="ISIN_B", type=StockTransactionType.BUY, amount=Decimal(1), price_per_unit=Decimal(10), fees=Decimal(0), executed_at=datetime.now()
    ), master_key)
    create_stock_transaction(session, StockTransactionCreate(
        account_id=acc2, symbol="C", isin="ISIN_C", type=StockTransactionType.BUY, amount=Decimal(1), price_per_unit=Decimal(10), fees=Decimal(0), executed_at=datetime.now()
    ), master_key)
    txs_1 = get_account_transactions(session, acc1, master_key)
    assert len(txs_1) == 2
    symbols = {t.symbol for t in txs_1}
    assert symbols == {"A", "B"}
    txs_2 = get_account_transactions(session, acc2, master_key)
    assert len(txs_2) == 1
    assert txs_2[0].symbol == "C"


@patch("services.stock_transaction.get_stock_info")
def test_get_stock_account_summary(mock_market, session: Session, master_key: str):
    # Side effect now receives (session, isin)
    mock_market.side_effect = lambda s, isin: {
        "ISIN_AAPL": ("Apple Inc.", Decimal("180.0")),
        "ISIN_MSFT": ("Microsoft", Decimal("300.0")),
        "ISIN_SOLD": ("Sold Stock", Decimal("10.0")),
    }.get(isin, ("Unknown", Decimal("0")))
    
    account = StockAccount(
        uuid="acc_main",
        user_uuid_bidx=hash_index("user_1", master_key),
        name_enc=encrypt_data("My PEA", master_key),
        account_type_enc=encrypt_data("PEA", master_key),
    )
    session.add(account)
    session.commit()
    create_stock_transaction(session, StockTransactionCreate(
        account_id="acc_main", symbol="AAPL", isin="ISIN_AAPL", type=StockTransactionType.BUY, amount=Decimal("10"), price_per_unit=Decimal("150"), fees=Decimal("5"), executed_at=datetime(2023, 1, 1)
    ), master_key)
    create_stock_transaction(session, StockTransactionCreate(
        account_id="acc_main", symbol="AAPL", isin="ISIN_AAPL", type=StockTransactionType.BUY, amount=Decimal("5"), price_per_unit=Decimal("160"), fees=Decimal("2"), executed_at=datetime(2023, 1, 2)
    ), master_key)
    create_stock_transaction(session, StockTransactionCreate(
        account_id="acc_main", symbol="MSFT", isin="ISIN_MSFT", type=StockTransactionType.BUY, amount=Decimal("10"), price_per_unit=Decimal("250"), fees=Decimal("5"), executed_at=datetime(2023, 1, 3)
    ), master_key)
    create_stock_transaction(session, StockTransactionCreate(
        account_id="acc_main", symbol="MSFT", isin="ISIN_MSFT", type=StockTransactionType.SELL, amount=Decimal("5"), price_per_unit=Decimal("280"), fees=Decimal("5"), executed_at=datetime(2023, 1, 4)
    ), master_key)
    create_stock_transaction(session, StockTransactionCreate(
        account_id="acc_main", symbol="SOLD", isin="ISIN_SOLD", type=StockTransactionType.BUY, amount=Decimal("10"), price_per_unit=Decimal("10"), fees=Decimal("1"), executed_at=datetime(2023, 1, 5)
    ), master_key)
    create_stock_transaction(session, StockTransactionCreate(
        account_id="acc_main", symbol="SOLD", isin="ISIN_SOLD", type=StockTransactionType.SELL, amount=Decimal("10"), price_per_unit=Decimal("12"), fees=Decimal("1"), executed_at=datetime(2023, 1, 6)
    ), master_key)
    summary = get_stock_account_summary(session, account, master_key)
    assert summary.account_name == "My PEA"
    pos_aapl = next(p for p in summary.positions if p.symbol == "AAPL")
    assert pos_aapl.total_amount == Decimal("15")
    assert pos_aapl.total_invested == Decimal("2307")
    assert pos_aapl.average_buy_price == round(Decimal("2307") / 15, 4)
    assert pos_aapl.current_value == Decimal("2700")
    assert pos_aapl.profit_loss == Decimal("393")
    pos_msft = next(p for p in summary.positions if p.symbol == "MSFT")
    assert pos_msft.total_amount == Decimal("5")
    assert pos_msft.total_invested == Decimal("1252.5")
    assert pos_msft.current_value == Decimal("1500")
    pos_sold = next((p for p in summary.positions if p.symbol == "SOLD"), None)
    assert pos_sold is None
    assert summary.total_invested == Decimal("3559.5")