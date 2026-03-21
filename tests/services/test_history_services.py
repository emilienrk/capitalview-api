"""
Tests for the history-reading functions across all account types:
  - get_stock_account_history / get_all_stock_accounts_history
  - get_crypto_account_history / get_all_crypto_accounts_history
  - get_bank_account_history / get_all_bank_accounts_history
  - get_asset_portfolio_history
"""

import json
import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlmodel import Session

from models.account_history import AccountHistory
from models.enums import AccountCategory, StockAccountType, BankAccountType
from services.encryption import encrypt_data, hash_index
from services.stock_account import (
    create_stock_account,
    get_stock_account_history,
    get_all_stock_accounts_history,
)
from services.crypto_account import (
    create_crypto_account,
    get_crypto_account_history,
    get_all_crypto_accounts_history,
)
from services.bank import (
    create_bank_account,
    get_bank_account_history,
    get_all_bank_accounts_history,
)
from services.asset import get_asset_portfolio_history
from dtos.stock import StockAccountCreate
from dtos.crypto import CryptoAccountCreate
from dtos import BankAccountCreate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _insert_history_row(
    session: Session,
    *,
    user_bidx: str,
    account_id_bidx: str,
    account_type: AccountCategory,
    snapshot_date: date,
    total_value: str,
    total_invested: str,
    master_key: str,
    daily_pnl: str = "0.00",
    positions: list | None = None,
) -> AccountHistory:
    """Insert a minimal AccountHistory row directly into the session."""
    positions_enc = None
    if positions is not None:
        positions_enc = encrypt_data(json.dumps(positions), master_key)

    row = AccountHistory(
        uuid=str(uuid.uuid4()),
        user_uuid_bidx=user_bidx,
        account_id_bidx=account_id_bidx,
        account_type=account_type,
        snapshot_date=snapshot_date,
        total_value_enc=encrypt_data(total_value, master_key),
        total_invested_enc=encrypt_data(total_invested, master_key),
        daily_pnl_enc=encrypt_data(daily_pnl, master_key),
        positions_enc=positions_enc,
    )
    session.add(row)
    session.commit()
    return row


# ---------------------------------------------------------------------------
# get_stock_account_history
# ---------------------------------------------------------------------------

class TestGetStockAccountHistory:
    def test_empty_when_no_snapshots(self, session: Session, master_key: str):
        acc = create_stock_account(session, StockAccountCreate(name="PEA", account_type=StockAccountType.PEA), "user_1", master_key)
        result = get_stock_account_history(session, acc.id, master_key)
        assert result == []

    def test_returns_snapshots_in_date_order(self, session: Session, master_key: str):
        acc = create_stock_account(session, StockAccountCreate(name="CTO", account_type=StockAccountType.CTO), "user_1", master_key)
        account_id_bidx = hash_index(acc.id, master_key)
        user_bidx = hash_index("user_1", master_key)

        _insert_history_row(session, user_bidx=user_bidx, account_id_bidx=account_id_bidx,
                            account_type=AccountCategory.STOCK, snapshot_date=date(2026, 1, 3),
                            total_value="1100.00", total_invested="1000.00", master_key=master_key)
        _insert_history_row(session, user_bidx=user_bidx, account_id_bidx=account_id_bidx,
                            account_type=AccountCategory.STOCK, snapshot_date=date(2026, 1, 1),
                            total_value="1000.00", total_invested="1000.00", master_key=master_key)
        _insert_history_row(session, user_bidx=user_bidx, account_id_bidx=account_id_bidx,
                            account_type=AccountCategory.STOCK, snapshot_date=date(2026, 1, 2),
                            total_value="1050.00", total_invested="1000.00", master_key=master_key)

        result = get_stock_account_history(session, acc.id, master_key)

        assert len(result) == 3
        assert result[0].snapshot_date == date(2026, 1, 1)
        assert result[1].snapshot_date == date(2026, 1, 2)
        assert result[2].snapshot_date == date(2026, 1, 3)

    def test_values_are_correctly_decrypted(self, session: Session, master_key: str):
        acc = create_stock_account(session, StockAccountCreate(name="PEA", account_type=StockAccountType.PEA), "user_1", master_key)
        account_id_bidx = hash_index(acc.id, master_key)
        user_bidx = hash_index("user_1", master_key)

        _insert_history_row(session, user_bidx=user_bidx, account_id_bidx=account_id_bidx,
                            account_type=AccountCategory.STOCK, snapshot_date=date(2026, 3, 1),
                            total_value="5432.10", total_invested="4000.00",
                            daily_pnl="123.45", master_key=master_key)

        result = get_stock_account_history(session, acc.id, master_key)

        assert len(result) == 1
        snap = result[0]
        assert snap.total_value == Decimal("5432.10")
        assert snap.total_invested == Decimal("4000.00")
        assert snap.daily_pnl == Decimal("123.45")

    def test_positions_are_parsed(self, session: Session, master_key: str):
        acc = create_stock_account(session, StockAccountCreate(name="PEA", account_type=StockAccountType.PEA), "user_1", master_key)
        account_id_bidx = hash_index(acc.id, master_key)
        user_bidx = hash_index("user_1", master_key)

        positions = [
            {"symbol": "AAPL", "quantity": "10", "value": "1500.00",
             "price": "150.00", "invested": "1200.00", "percentage": "100.00"}
        ]
        _insert_history_row(session, user_bidx=user_bidx, account_id_bidx=account_id_bidx,
                            account_type=AccountCategory.STOCK, snapshot_date=date(2026, 3, 1),
                            total_value="1500.00", total_invested="1200.00",
                            master_key=master_key, positions=positions)

        result = get_stock_account_history(session, acc.id, master_key)

        assert result[0].positions is not None
        assert len(result[0].positions) == 1
        pos = result[0].positions[0]
        assert pos.symbol == "AAPL"
        assert pos.quantity == Decimal("10")
        assert pos.price == Decimal("150.00")
        assert pos.percentage == Decimal("100.00")

    def test_null_price_in_position(self, session: Session, master_key: str):
        """A position with price=null in JSON should map to None, not raise."""
        acc = create_stock_account(session, StockAccountCreate(name="CTO", account_type=StockAccountType.CTO), "user_2", master_key)
        account_id_bidx = hash_index(acc.id, master_key)
        user_bidx = hash_index("user_2", master_key)

        positions = [
            {"symbol": "BTC", "quantity": "1", "value": "0.00",
             "price": None, "invested": "30000.00", "percentage": "0.00"}
        ]
        _insert_history_row(session, user_bidx=user_bidx, account_id_bidx=account_id_bidx,
                            account_type=AccountCategory.STOCK, snapshot_date=date(2026, 3, 1),
                            total_value="0.00", total_invested="30000.00",
                            master_key=master_key, positions=positions)

        result = get_stock_account_history(session, acc.id, master_key)
        assert result[0].positions[0].price is None

    def test_no_cross_account_leakage(self, session: Session, master_key: str):
        """History for account A must not return rows from account B."""
        acc_a = create_stock_account(session, StockAccountCreate(name="A", account_type=StockAccountType.PEA), "user_1", master_key)
        acc_b = create_stock_account(session, StockAccountCreate(name="B", account_type=StockAccountType.CTO), "user_1", master_key)

        user_bidx = hash_index("user_1", master_key)
        _insert_history_row(session, user_bidx=user_bidx, account_id_bidx=hash_index(acc_b.id, master_key),
                            account_type=AccountCategory.STOCK, snapshot_date=date(2026, 1, 1),
                            total_value="9999.00", total_invested="9999.00", master_key=master_key)

        result = get_stock_account_history(session, acc_a.id, master_key)
        assert result == []


# ---------------------------------------------------------------------------
# get_all_stock_accounts_history
# ---------------------------------------------------------------------------

class TestGetAllStockAccountsHistory:
    def test_empty_when_no_accounts(self, session: Session, master_key: str):
        result = get_all_stock_accounts_history(session, "user_with_no_accounts", master_key)
        assert result == []

    def test_single_account_passthrough(self, session: Session, master_key: str):
        user = "user_single"
        acc = create_stock_account(session, StockAccountCreate(name="PEA", account_type=StockAccountType.PEA), user, master_key)
        user_bidx = hash_index(user, master_key)

        _insert_history_row(session, user_bidx=user_bidx, account_id_bidx=hash_index(acc.id, master_key),
                            account_type=AccountCategory.STOCK, snapshot_date=date(2026, 1, 1),
                            total_value="2000.00", total_invested="1800.00", master_key=master_key)

        result = get_all_stock_accounts_history(session, user, master_key)
        assert len(result) == 1
        assert result[0].total_value == Decimal("2000.00")
        assert result[0].total_invested == Decimal("1800.00")

    def test_multiple_accounts_same_date_are_summed(self, session: Session, master_key: str):
        user = "user_multi"
        acc1 = create_stock_account(session, StockAccountCreate(name="PEA", account_type=StockAccountType.PEA), user, master_key)
        acc2 = create_stock_account(session, StockAccountCreate(name="CTO", account_type=StockAccountType.CTO), user, master_key)
        user_bidx = hash_index(user, master_key)

        _insert_history_row(session, user_bidx=user_bidx, account_id_bidx=hash_index(acc1.id, master_key),
                            account_type=AccountCategory.STOCK, snapshot_date=date(2026, 2, 1),
                            total_value="3000.00", total_invested="2500.00", master_key=master_key)
        _insert_history_row(session, user_bidx=user_bidx, account_id_bidx=hash_index(acc2.id, master_key),
                            account_type=AccountCategory.STOCK, snapshot_date=date(2026, 2, 1),
                            total_value="1500.00", total_invested="1200.00", master_key=master_key)

        result = get_all_stock_accounts_history(session, user, master_key)
        assert len(result) == 1
        assert result[0].total_value == Decimal("4500.00")
        assert result[0].total_invested == Decimal("3700.00")

    def test_union_of_dates_across_accounts(self, session: Session, master_key: str):
        """Account A has day 1-2, account B has day 2-3 → result has 3 distinct dates."""
        user = "user_union"
        acc1 = create_stock_account(session, StockAccountCreate(name="PEA", account_type=StockAccountType.PEA), user, master_key)
        acc2 = create_stock_account(session, StockAccountCreate(name="CTO", account_type=StockAccountType.CTO), user, master_key)
        user_bidx = hash_index(user, master_key)

        for d in [date(2026, 3, 1), date(2026, 3, 2)]:
            _insert_history_row(session, user_bidx=user_bidx, account_id_bidx=hash_index(acc1.id, master_key),
                                account_type=AccountCategory.STOCK, snapshot_date=d,
                                total_value="1000.00", total_invested="1000.00", master_key=master_key)
        for d in [date(2026, 3, 2), date(2026, 3, 3)]:
            _insert_history_row(session, user_bidx=user_bidx, account_id_bidx=hash_index(acc2.id, master_key),
                                account_type=AccountCategory.STOCK, snapshot_date=d,
                                total_value="500.00", total_invested="500.00", master_key=master_key)

        result = get_all_stock_accounts_history(session, user, master_key)
        dates = [s.snapshot_date for s in result]
        assert dates == [date(2026, 3, 1), date(2026, 3, 2), date(2026, 3, 3)]
        # day 2 has both accounts
        assert result[1].total_value == Decimal("1500.00")
        # day 1 has only acc1
        assert result[0].total_value == Decimal("1000.00")
        # day 3 has only acc2
        assert result[2].total_value == Decimal("500.00")

    def test_positions_percentage_recalculated_after_aggregation(self, session: Session, master_key: str):
        """After summing two accounts holding the same symbol, percentage must be recomputed on the combined total."""
        user = "user_pct"
        acc1 = create_stock_account(session, StockAccountCreate(name="PEA", account_type=StockAccountType.PEA), user, master_key)
        acc2 = create_stock_account(session, StockAccountCreate(name="CTO", account_type=StockAccountType.CTO), user, master_key)
        user_bidx = hash_index(user, master_key)

        for acc_id, value in [(acc1.id, "400.00"), (acc2.id, "600.00")]:
            positions = [{"symbol": "MSFT", "quantity": "1", "value": value,
                          "price": value, "invested": value, "percentage": "100.00"}]
            _insert_history_row(session, user_bidx=user_bidx, account_id_bidx=hash_index(acc_id, master_key),
                                account_type=AccountCategory.STOCK, snapshot_date=date(2026, 3, 1),
                                total_value=value, total_invested=value, master_key=master_key, positions=positions)

        result = get_all_stock_accounts_history(session, user, master_key)
        assert result[0].total_value == Decimal("1000.00")
        msft = result[0].positions[0]
        assert msft.symbol == "MSFT"
        assert msft.value == Decimal("1000.00")
        # percentage relative to the combined total
        assert msft.percentage == Decimal("100.00")

    def test_other_user_data_not_included(self, session: Session, master_key: str):
        user_a = "user_a"
        user_b = "user_b"
        acc_a = create_stock_account(session, StockAccountCreate(name="PEA", account_type=StockAccountType.PEA), user_a, master_key)
        acc_b = create_stock_account(session, StockAccountCreate(name="PEA", account_type=StockAccountType.PEA), user_b, master_key)

        for user, acc in [(user_a, acc_a), (user_b, acc_b)]:
            _insert_history_row(session, user_bidx=hash_index(user, master_key),
                                account_id_bidx=hash_index(acc.id, master_key),
                                account_type=AccountCategory.STOCK, snapshot_date=date(2026, 1, 1),
                                total_value="5000.00", total_invested="5000.00", master_key=master_key)

        result_a = get_all_stock_accounts_history(session, user_a, master_key)
        assert len(result_a) == 1
        assert result_a[0].total_value == Decimal("5000.00")


# ---------------------------------------------------------------------------
# get_crypto_account_history / get_all_crypto_accounts_history
# ---------------------------------------------------------------------------

class TestCryptoAccountHistory:
    def test_single_account_history(self, session: Session, master_key: str):
        acc = create_crypto_account(session, CryptoAccountCreate(name="Ledger"), "user_c", master_key)
        user_bidx = hash_index("user_c", master_key)

        _insert_history_row(session, user_bidx=user_bidx, account_id_bidx=hash_index(acc.id, master_key),
                            account_type=AccountCategory.CRYPTO, snapshot_date=date(2026, 1, 1),
                            total_value="8000.00", total_invested="5000.00",
                            daily_pnl="200.00", master_key=master_key)

        result = get_crypto_account_history(session, acc.id, master_key)
        assert len(result) == 1
        assert result[0].total_value == Decimal("8000.00")
        assert result[0].daily_pnl == Decimal("200.00")

    def test_all_accounts_aggregation(self, session: Session, master_key: str):
        user = "user_crypto_agg"
        acc1 = create_crypto_account(session, CryptoAccountCreate(name="Binance"), user, master_key)
        acc2 = create_crypto_account(session, CryptoAccountCreate(name="Cold Wallet"), user, master_key)
        user_bidx = hash_index(user, master_key)

        _insert_history_row(session, user_bidx=user_bidx, account_id_bidx=hash_index(acc1.id, master_key),
                            account_type=AccountCategory.CRYPTO, snapshot_date=date(2026, 1, 1),
                            total_value="10000.00", total_invested="8000.00", master_key=master_key)
        _insert_history_row(session, user_bidx=user_bidx, account_id_bidx=hash_index(acc2.id, master_key),
                            account_type=AccountCategory.CRYPTO, snapshot_date=date(2026, 1, 1),
                            total_value="4000.00", total_invested="3000.00", master_key=master_key)

        result = get_all_crypto_accounts_history(session, user, master_key)
        assert len(result) == 1
        assert result[0].total_value == Decimal("14000.00")
        assert result[0].total_invested == Decimal("11000.00")


# ---------------------------------------------------------------------------
# get_bank_account_history / get_all_bank_accounts_history
# ---------------------------------------------------------------------------

class TestBankAccountHistory:
    def test_single_account_history(self, session: Session, master_key: str):
        acc = create_bank_account(session, BankAccountCreate(name="Livret A", balance=Decimal("10000"), account_type=BankAccountType.LIVRET_A), "user_b", master_key)
        user_bidx = hash_index("user_b", master_key)

        _insert_history_row(session, user_bidx=user_bidx, account_id_bidx=hash_index(acc.id, master_key),
                            account_type=AccountCategory.BANK, snapshot_date=date(2026, 1, 1),
                            total_value="10000.00", total_invested="10000.00", master_key=master_key)

        result = get_bank_account_history(session, acc.id, master_key)
        assert len(result) == 1
        assert result[0].total_value == Decimal("10000.00")

    def test_all_accounts_aggregation_eur_position(self, session: Session, master_key: str):
        user = "user_bank_agg"
        acc1 = create_bank_account(session, BankAccountCreate(name="Checking", balance=Decimal("5000"), account_type=BankAccountType.CHECKING), user, master_key)
        acc2 = create_bank_account(session, BankAccountCreate(name="Savings", balance=Decimal("3000"), account_type=BankAccountType.SAVINGS), user, master_key)
        user_bidx = hash_index(user, master_key)

        d = date(2026, 2, 1)
        for acc, value in [(acc1, "5000.00"), (acc2, "3000.00")]:
            positions = [{"symbol": "EUR", "quantity": value, "value": value,
                          "price": "1.00", "invested": value, "percentage": "100.00"}]
            _insert_history_row(session, user_bidx=user_bidx, account_id_bidx=hash_index(acc.id, master_key),
                                account_type=AccountCategory.BANK, snapshot_date=d,
                                total_value=value, total_invested=value,
                                master_key=master_key, positions=positions)

        result = get_all_bank_accounts_history(session, user, master_key)
        assert len(result) == 1
        snap = result[0]
        assert snap.total_value == Decimal("8000.00")
        assert snap.total_invested == Decimal("8000.00")
        # Aggregated EUR position
        assert snap.positions is not None
        eur_pos = snap.positions[0]
        assert eur_pos.symbol == "EUR"
        assert eur_pos.value == Decimal("8000.00")
        assert eur_pos.percentage == Decimal("100")

    def test_empty_when_no_history(self, session: Session, master_key: str):
        acc = create_bank_account(session, BankAccountCreate(name="Empty", balance=Decimal("0"), account_type=BankAccountType.CHECKING), "user_empty_bank", master_key)
        result = get_bank_account_history(session, acc.id, master_key)
        assert result == []


# ---------------------------------------------------------------------------
# get_asset_portfolio_history
# ---------------------------------------------------------------------------

class TestAssetPortfolioHistory:
    def _virtual_account_id_bidx(self, user_uuid: str, master_key: str) -> str:
        """Reproduce the virtual account ID logic from services/asset.py."""
        user_bidx = hash_index(user_uuid, master_key)
        virtual_id = f"ASSET_PORTFOLIO::{user_bidx}"
        return hash_index(virtual_id, master_key)

    def test_empty_when_no_history(self, session: Session, master_key: str):
        result = get_asset_portfolio_history(session, "user_no_assets", master_key)
        assert result == []

    def test_returns_snapshots_in_order(self, session: Session, master_key: str):
        user = "user_assets"
        user_bidx = hash_index(user, master_key)
        account_id_bidx = self._virtual_account_id_bidx(user, master_key)

        for d, value in [(date(2026, 1, 3), "150000.00"), (date(2026, 1, 1), "130000.00"), (date(2026, 1, 2), "140000.00")]:
            _insert_history_row(session, user_bidx=user_bidx, account_id_bidx=account_id_bidx,
                                account_type=AccountCategory.ASSET, snapshot_date=d,
                                total_value=value, total_invested="100000.00", master_key=master_key)

        result = get_asset_portfolio_history(session, user, master_key)
        assert len(result) == 3
        assert result[0].snapshot_date == date(2026, 1, 1)
        assert result[2].total_value == Decimal("150000.00")

    def test_positions_decrypted(self, session: Session, master_key: str):
        user = "user_assets_pos"
        user_bidx = hash_index(user, master_key)
        account_id_bidx = self._virtual_account_id_bidx(user, master_key)

        positions = [
            {"symbol": "Appartement Paris", "quantity": "1", "value": "350000.00",
             "price": "350000.00", "invested": "300000.00", "percentage": "100.00"}
        ]
        _insert_history_row(session, user_bidx=user_bidx, account_id_bidx=account_id_bidx,
                            account_type=AccountCategory.ASSET, snapshot_date=date(2026, 3, 1),
                            total_value="350000.00", total_invested="300000.00",
                            master_key=master_key, positions=positions)

        result = get_asset_portfolio_history(session, user, master_key)
        assert len(result) == 1
        assert result[0].positions[0].symbol == "Appartement Paris"
        assert result[0].positions[0].value == Decimal("350000.00")

    def test_does_not_read_other_users_data(self, session: Session, master_key: str):
        """Different users produce different virtual account IDs."""
        user_a = "user_asset_a"
        user_b = "user_asset_b"

        user_b_bidx = hash_index(user_b, master_key)
        account_id_bidx_b = self._virtual_account_id_bidx(user_b, master_key)

        _insert_history_row(session, user_bidx=user_b_bidx, account_id_bidx=account_id_bidx_b,
                            account_type=AccountCategory.ASSET, snapshot_date=date(2026, 1, 1),
                            total_value="200000.00", total_invested="150000.00", master_key=master_key)

        result_a = get_asset_portfolio_history(session, user_a, master_key)
        assert result_a == []
