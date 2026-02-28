"""
Tests for the Binance CSV import service.

Covers:
  - CSV parsing (_parse_csv)
  - Row mapping (_map_row) for every supported operation
  - Preview generation (generate_preview): grouping, EUR detection, needs_eur_input
  - Import execution (execute_import): correct atomic rows & FIAT_ANCHOR injection
  - PRU compatibility: verifies cost basis is correctly computed after import
"""

import textwrap
from decimal import Decimal
from datetime import datetime
from unittest.mock import patch

import pytest
from sqlmodel import Session

from services.imports.binance import (
    _parse_csv,
    _map_row,
    _BinanceRow,
    generate_preview,
    execute_import,
)
from services.crypto_transaction import get_crypto_account_summary
from models.enums import CryptoTransactionType
from models.crypto import CryptoAccount
from dtos.crypto import BinanceImportGroupPreview, BinanceImportRowPreview
from services.encryption import encrypt_data, hash_index


# ── Helpers ────────────────────────────────────────────────────

def _make_row(
    operation: str,
    coin: str,
    change: str,
    utc_time: str = "2024-01-01 12:00:00",
) -> _BinanceRow:
    from decimal import Decimal as D
    from datetime import datetime as dt
    return _BinanceRow(
        utc_time=dt.fromisoformat(utc_time.replace(" ", "T")),
        account="Spot",
        operation=operation,
        coin=coin,
        change=D(change),
        remark="",
    )


def _csv(rows: list[str]) -> str:
    """Build a minimal Binance CSV string."""
    header = "User_ID,UTC_Time,Account,Operation,Coin,Change,Remark"
    lines = [header] + rows
    return "\n".join(lines)


# ── CSV Parsing ────────────────────────────────────────────────

class TestParseCSV:
    def test_basic_row(self):
        content = _csv([
            "123,2024-01-01 10:00:00,Spot,Buy Crypto With Fiat,BTC,0.001,",
        ])
        rows = _parse_csv(content)
        assert len(rows) == 1
        assert rows[0].coin == "BTC"
        assert rows[0].change == Decimal("0.001")
        assert rows[0].operation == "Buy Crypto With Fiat"

    def test_skips_zero_change(self):
        content = _csv([
            "123,2024-01-01 10:00:00,Spot,Deposit,BTC,0.0,",
            "123,2024-01-01 10:00:00,Spot,Deposit,ETH,1.0,",
        ])
        rows = _parse_csv(content)
        assert len(rows) == 1
        assert rows[0].coin == "ETH"

    def test_skips_invalid_date(self):
        content = _csv([
            "123,INVALID,Spot,Deposit,BTC,1.0,",
        ])
        rows = _parse_csv(content)
        assert rows == []

    def test_scientific_notation(self):
        content = _csv([
            "123,2024-01-01 10:00:00,Spot,Transaction Fee,BNB,6.8E-7,",
        ])
        rows = _parse_csv(content)
        assert len(rows) == 1
        assert rows[0].change == Decimal("6.8E-7")

    def test_strips_bom(self):
        content = "\ufeff" + _csv([
            "123,2024-01-01 10:00:00,Spot,Deposit,BTC,1.0,",
        ])
        rows = _parse_csv(content)
        assert len(rows) == 1

    def test_multiple_rows(self):
        content = _csv([
            "123,2024-01-01 10:00:00,Spot,Transaction Buy,BTC,0.1,",
            "123,2024-01-01 10:00:01,Spot,Transaction Spend,EUR,-3000,",
            "123,2024-01-01 10:00:01,Spot,Transaction Fee,BNB,-0.01,",
        ])
        rows = _parse_csv(content)
        assert len(rows) == 3


# ── Row Mapping ────────────────────────────────────────────────

class TestMapRow:
    """Verify each Binance operation maps to the correct atomic type + price."""

    # ── Deposit ──

    def test_deposit_crypto(self):
        row = _make_row("Deposit", "BTC", "0.5")
        t, sym, amt, price = _map_row(row)
        assert t == CryptoTransactionType.BUY
        assert sym == "BTC"
        assert price == Decimal("0")

    def test_deposit_eur(self):
        row = _make_row("Deposit", "EUR", "1000")
        t, sym, amt, price = _map_row(row)
        assert t == CryptoTransactionType.FIAT_DEPOSIT
        assert sym == "EUR"
        assert price == Decimal("1")

    # ── Withdraw ──

    def test_withdraw(self):
        row = _make_row("Withdraw", "BTC", "-0.1")
        t, sym, amt, price = _map_row(row)
        assert t == CryptoTransactionType.TRANSFER
        assert price == Decimal("0")

    # ── Buy Crypto With Fiat ──

    def test_buy_crypto_with_fiat_crypto_leg(self):
        row = _make_row("Buy Crypto With Fiat", "BTC", "0.1")
        t, sym, amt, price = _map_row(row)
        assert t == CryptoTransactionType.BUY
        assert price == Decimal("0")

    def test_buy_crypto_with_fiat_eur_leg(self):
        row = _make_row("Buy Crypto With Fiat", "EUR", "-3000")
        t, sym, amt, price = _map_row(row)
        assert t == CryptoTransactionType.SPEND
        assert sym == "EUR"
        assert price == Decimal("1")

    # ── Crypto Box ──

    def test_crypto_box_reward(self):
        row = _make_row("Crypto Box", "BTC", "0.01")
        t, sym, amt, price = _map_row(row)
        assert t == CryptoTransactionType.REWARD
        assert price == Decimal("0")

    # ── Binance Convert ──

    def test_binance_convert_incoming_crypto(self):
        row = _make_row("Binance Convert", "BTC", "0.1")  # receiving BTC
        t, sym, amt, price = _map_row(row)
        assert t == CryptoTransactionType.BUY
        assert price == Decimal("0")

    def test_binance_convert_incoming_eur(self):
        row = _make_row("Binance Convert", "EUR", "3000")  # receiving EUR
        t, sym, amt, price = _map_row(row)
        assert t == CryptoTransactionType.FIAT_DEPOSIT
        assert price == Decimal("1")

    def test_binance_convert_outgoing_crypto(self):
        row = _make_row("Binance Convert", "USDC", "-3000")  # spending USDC
        t, sym, amt, price = _map_row(row)
        assert t == CryptoTransactionType.SPEND
        assert sym == "USDC"
        assert price == Decimal("0")

    def test_binance_convert_outgoing_eur(self):
        row = _make_row("Binance Convert", "EUR", "-3000")  # spending EUR
        t, sym, amt, price = _map_row(row)
        assert t == CryptoTransactionType.SPEND
        assert sym == "EUR"
        assert price == Decimal("1")

    # ── Transaction Buy / Spend / Fee / Sold / Revenue ──

    def test_transaction_buy(self):
        row = _make_row("Transaction Buy", "BTC", "0.1")
        t, sym, amt, price = _map_row(row)
        assert t == CryptoTransactionType.BUY
        assert price == Decimal("0")

    def test_transaction_spend_crypto(self):
        row = _make_row("Transaction Spend", "USDC", "-3000")
        t, sym, amt, price = _map_row(row)
        assert t == CryptoTransactionType.SPEND
        assert price == Decimal("0")

    def test_transaction_spend_eur(self):
        row = _make_row("Transaction Spend", "EUR", "-3000")
        t, sym, amt, price = _map_row(row)
        assert t == CryptoTransactionType.SPEND
        assert price == Decimal("1")

    def test_transaction_fee(self):
        row = _make_row("Transaction Fee", "BNB", "-0.01")
        t, sym, amt, price = _map_row(row)
        assert t == CryptoTransactionType.FEE
        assert price == Decimal("0")

    def test_transaction_sold_crypto(self):
        """Selling BTC maps to SPEND with price=0."""
        row = _make_row("Transaction Sold", "BTC", "-0.1")
        t, sym, amt, price = _map_row(row)
        assert t == CryptoTransactionType.SPEND
        assert price == Decimal("0")

    def test_transaction_revenue_eur(self):
        """Receiving EUR from a sale = FIAT_DEPOSIT at price=1."""
        row = _make_row("Transaction Revenue", "EUR", "3000")
        t, sym, amt, price = _map_row(row)
        assert t == CryptoTransactionType.FIAT_DEPOSIT
        assert price == Decimal("1")

    def test_transaction_revenue_crypto(self):
        """Receiving crypto via revenue (e.g. staking) → BUY price=0."""
        row = _make_row("Transaction Revenue", "BTC", "0.01")
        t, sym, amt, price = _map_row(row)
        assert t == CryptoTransactionType.BUY
        assert price == Decimal("0")


# ── Preview Generation ─────────────────────────────────────────

class TestGeneratePreview:
    """generate_preview grouping, EUR detection, and needs_eur_input."""

    def test_empty_csv(self):
        resp = generate_preview("")
        assert resp.total_groups == 0
        assert resp.total_rows == 0

    def test_eur_trade_no_anchor_needed(self):
        """Buy BTC with EUR: has_eur=True → needs_eur_input=False."""
        content = _csv([
            "1,2024-01-01 12:00:00,Spot,Buy Crypto With Fiat,BTC,0.1,",
            "1,2024-01-01 12:00:00,Spot,Buy Crypto With Fiat,EUR,-3000,",
        ])
        resp = generate_preview(content)
        assert resp.total_groups == 1
        g = resp.groups[0]
        assert g.has_eur is True
        assert g.needs_eur_input is False
        assert g.auto_eur_amount == pytest.approx(3000.0)

    def test_crypto_swap_needs_eur(self):
        """Swap BTC→USDC via Binance Convert: no EUR → needs_eur_input=True."""
        content = _csv([
            "1,2024-01-01 12:00:00,Spot,Binance Convert,BTC,-0.1,",
            "1,2024-01-01 12:00:01,Spot,Binance Convert,USDC,2800,",
        ])
        resp = generate_preview(content)
        assert resp.total_groups == 1
        assert resp.groups_needing_eur == 1
        g = resp.groups[0]
        assert g.needs_eur_input is True
        assert g.has_eur is False

    def test_reward_no_anchor_needed(self):
        """Crypto Box reward: reward-only group never needs EUR."""
        content = _csv([
            "1,2024-01-01 12:00:00,Spot,Crypto Box,BTC,0.001,",
        ])
        resp = generate_preview(content)
        g = resp.groups[0]
        assert g.needs_eur_input is False

    def test_withdraw_no_anchor_needed(self):
        """Withdraw = TRANSFER, transfer-only group never needs EUR."""
        content = _csv([
            "1,2024-01-01 12:00:00,Spot,Withdraw,BTC,0.1,",
        ])
        resp = generate_preview(content)
        g = resp.groups[0]
        assert g.needs_eur_input is False

    def test_crypto_deposit_needs_eur(self):
        """Deposit crypto (external wallet): needs EUR input for cost basis."""
        content = _csv([
            "1,2024-01-01 12:00:00,Spot,Deposit,BTC,0.5,",
        ])
        resp = generate_preview(content)
        g = resp.groups[0]
        assert g.needs_eur_input is True

    def test_rows_sorted_buy_first(self):
        """Mapped rows inside a group are sorted BUY before SPEND before FEE."""
        content = _csv([
            "1,2024-01-01 12:00:00,Spot,Transaction Fee,BNB,-0.01,",
            "1,2024-01-01 12:00:00,Spot,Transaction Spend,EUR,-3000,",
            "1,2024-01-01 12:00:00,Spot,Transaction Buy,BTC,0.1,",
        ])
        resp = generate_preview(content)
        types = [r.mapped_type for r in resp.groups[0].rows]
        assert types.index("BUY") < types.index("SPEND")
        assert types.index("SPEND") < types.index("FEE")

    def test_grouping_within_6_seconds(self):
        """Rows ≤6 s apart share a group; rows >6 s apart are separate."""
        content = _csv([
            "1,2024-01-01 12:00:00,Spot,Transaction Buy,BTC,0.1,",
            "1,2024-01-01 12:00:05,Spot,Transaction Spend,USDC,-3000,",  # +5 s → same group
            "1,2024-01-01 12:00:10,Spot,Crypto Box,ETH,0.5,",           # +10 s → new group
        ])
        resp = generate_preview(content)
        assert resp.total_groups == 2

    def test_sell_btc_for_eur_no_anchor(self):
        """Sell BTC (Transaction Sold) + receive EUR (Transaction Revenue): has_eur=True."""
        content = _csv([
            "1,2024-01-01 12:00:00,Spot,Transaction Sold,BTC,-0.1,",
            "1,2024-01-01 12:00:00,Spot,Transaction Revenue,EUR,3000,",
        ])
        resp = generate_preview(content)
        g = resp.groups[0]
        assert g.has_eur is True
        assert g.needs_eur_input is False


# ── Import Execution ───────────────────────────────────────────

class TestExecuteImport:
    """execute_import creates atomic rows correctly and adds FIAT_ANCHOR when needed."""

    def _make_account(self, session: Session, master_key: str, account_id: str = "acc_test") -> CryptoAccount:
        account = CryptoAccount(
            uuid=account_id,
            user_uuid_bidx=hash_index("user_test", master_key),
            name_enc=encrypt_data("Test Account", master_key),
        )
        session.add(account)
        session.commit()
        return account

    def _make_group(
        self,
        rows: list[dict],
        *,
        group_index: int = 0,
        timestamp: str = "2024-01-01T12:00:00",
        has_eur: bool = False,
        needs_eur_input: bool = False,
        eur_amount: float | None = None,
    ) -> BinanceImportGroupPreview:
        mapped = [BinanceImportRowPreview(**r) for r in rows]
        return BinanceImportGroupPreview(
            group_index=group_index,
            timestamp=timestamp,
            rows=mapped,
            summary="test",
            has_eur=has_eur,
            needs_eur_input=needs_eur_input,
            eur_amount=eur_amount,
        )

    def test_buy_btc_with_eur_creates_two_rows(self, session: Session, master_key: str):
        """
        Buy BTC with EUR → BUY BTC (price=0) + SPEND EUR (price=1).
        No FIAT_ANCHOR added (EUR is already the anchor via SPEND EUR).
        """
        self._make_account(session, master_key)

        group = self._make_group(
            rows=[
                dict(operation="Buy Crypto With Fiat", coin="BTC", change=0.1,
                     mapped_type="BUY", mapped_symbol="BTC", mapped_amount=0.1, mapped_price=0.0),
                dict(operation="Buy Crypto With Fiat", coin="EUR", change=-3000,
                     mapped_type="SPEND", mapped_symbol="EUR", mapped_amount=3000.0, mapped_price=1.0),
            ],
            has_eur=True,
            needs_eur_input=False,
        )

        result = execute_import(session, "acc_test", [group], master_key)
        assert result.imported_count == 2
        assert result.groups_count == 1

    def test_crypto_swap_adds_fiat_anchor(self, session: Session, master_key: str):
        """
        BTC→USDC swap with user-provided EUR amount: adds FIAT_ANCHOR row.
        Total rows = BUY + SPEND + FIAT_ANCHOR = 3.
        """
        self._make_account(session, master_key)

        group = self._make_group(
            rows=[
                dict(operation="Binance Convert", coin="BTC", change=-0.1,
                     mapped_type="SPEND", mapped_symbol="BTC", mapped_amount=0.1, mapped_price=0.0),
                dict(operation="Binance Convert", coin="USDC", change=2800,
                     mapped_type="BUY", mapped_symbol="USDC", mapped_amount=2800.0, mapped_price=0.0),
            ],
            has_eur=False,
            needs_eur_input=True,
            eur_amount=2760.0,
        )

        result = execute_import(session, "acc_test", [group], master_key)
        assert result.imported_count == 3  # BUY + SPEND + FIAT_ANCHOR

    def test_no_anchor_when_no_eur_amount(self, session: Session, master_key: str):
        """
        Crypto swap but user did NOT provide EUR amount → no FIAT_ANCHOR added.
        """
        self._make_account(session, master_key)

        group = self._make_group(
            rows=[
                dict(operation="Binance Convert", coin="BTC", change=-0.1,
                     mapped_type="SPEND", mapped_symbol="BTC", mapped_amount=0.1, mapped_price=0.0),
                dict(operation="Binance Convert", coin="USDC", change=2800,
                     mapped_type="BUY", mapped_symbol="USDC", mapped_amount=2800.0, mapped_price=0.0),
            ],
            needs_eur_input=True,
            eur_amount=None,  # user skipped
        )

        result = execute_import(session, "acc_test", [group], master_key)
        assert result.imported_count == 2  # no FIAT_ANCHOR

    def test_reward_creates_one_row(self, session: Session, master_key: str):
        """Crypto Box → single REWARD row (price=0)."""
        self._make_account(session, master_key)

        group = self._make_group(
            rows=[
                dict(operation="Crypto Box", coin="BTC", change=0.001,
                     mapped_type="REWARD", mapped_symbol="BTC", mapped_amount=0.001, mapped_price=0.0),
            ],
        )

        result = execute_import(session, "acc_test", [group], master_key)
        assert result.imported_count == 1

    def test_sell_btc_for_eur_creates_two_rows(self, session: Session, master_key: str):
        """
        Sell BTC for EUR: SPEND BTC (price=0) + FIAT_DEPOSIT EUR (price=1).
        No anchor needed.
        """
        self._make_account(session, master_key)

        group = self._make_group(
            rows=[
                dict(operation="Transaction Sold", coin="BTC", change=-0.1,
                     mapped_type="SPEND", mapped_symbol="BTC", mapped_amount=0.1, mapped_price=0.0),
                dict(operation="Transaction Revenue", coin="EUR", change=3000,
                     mapped_type="FIAT_DEPOSIT", mapped_symbol="EUR", mapped_amount=3000.0, mapped_price=1.0),
            ],
            has_eur=True,
            needs_eur_input=False,
        )

        result = execute_import(session, "acc_test", [group], master_key)
        assert result.imported_count == 2

    def test_skips_zero_amount_rows(self, session: Session, master_key: str):
        """Rows with mapped_amount=0 are silently skipped."""
        self._make_account(session, master_key)

        group = self._make_group(
            rows=[
                dict(operation="Deposit", coin="BTC", change=0.1,
                     mapped_type="BUY", mapped_symbol="BTC", mapped_amount=0.0,  # zero
                     mapped_price=0.0),
                dict(operation="Deposit", coin="ETH", change=1.0,
                     mapped_type="BUY", mapped_symbol="ETH", mapped_amount=1.0, mapped_price=0.0),
            ],
        )

        result = execute_import(session, "acc_test", [group], master_key)
        assert result.imported_count == 1

    def test_transaction_with_fee(self, session: Session, master_key: str):
        """
        Buy BTC with EUR + BNB fee: BUY BTC + SPEND EUR + FEE BNB = 3 rows.
        """
        self._make_account(session, master_key)

        group = self._make_group(
            rows=[
                dict(operation="Transaction Buy", coin="BTC", change=0.1,
                     mapped_type="BUY", mapped_symbol="BTC", mapped_amount=0.1, mapped_price=0.0),
                dict(operation="Transaction Spend", coin="EUR", change=-3000,
                     mapped_type="SPEND", mapped_symbol="EUR", mapped_amount=3000.0, mapped_price=1.0),
                dict(operation="Transaction Fee", coin="BNB", change=-0.01,
                     mapped_type="FEE", mapped_symbol="BNB", mapped_amount=0.01, mapped_price=0.0),
            ],
            has_eur=True,
            needs_eur_input=False,
        )

        result = execute_import(session, "acc_test", [group], master_key)
        assert result.imported_count == 3

    def test_multiple_groups(self, session: Session, master_key: str):
        """Multiple groups are all imported independently."""
        self._make_account(session, master_key)

        g1 = self._make_group(
            rows=[
                dict(operation="Buy Crypto With Fiat", coin="BTC", change=0.1,
                     mapped_type="BUY", mapped_symbol="BTC", mapped_amount=0.1, mapped_price=0.0),
                dict(operation="Buy Crypto With Fiat", coin="EUR", change=-3000,
                     mapped_type="SPEND", mapped_symbol="EUR", mapped_amount=3000.0, mapped_price=1.0),
            ],
            group_index=0, has_eur=True,
        )
        g2 = self._make_group(
            rows=[
                dict(operation="Crypto Box", coin="ETH", change=0.5,
                     mapped_type="REWARD", mapped_symbol="ETH", mapped_amount=0.5, mapped_price=0.0),
            ],
            group_index=1, timestamp="2024-01-02T12:00:00",
        )

        result = execute_import(session, "acc_test", [g1, g2], master_key)
        assert result.imported_count == 3
        assert result.groups_count == 2


# ── PRU Compatibility ──────────────────────────────────────────

class TestPRUAfterImport:
    """
    Verifies that after an import, get_crypto_account_summary correctly
    computes cost basis (PRU) from the stored atomic rows.

    PRU rule (from CRYPTO_ACCOUNTING.md):
      group_cost = FIAT_ANCHOR.amount  OR  SPEND_EUR.amount
      PRU = group_cost / BUY.amount
    """

    def _make_account(self, session: Session, master_key: str, account_id: str = "acc_pru") -> CryptoAccount:
        account = CryptoAccount(
            uuid=account_id,
            user_uuid_bidx=hash_index("user_pru", master_key),
            name_enc=encrypt_data("PRU Test", master_key),
        )
        session.add(account)
        session.commit()
        return account

    @patch("services.crypto_transaction.get_crypto_info")
    def test_buy_with_eur_pru(self, mock_info, session: Session, master_key: str):
        """
        Buy 0.1 BTC for 3000 EUR → SPEND EUR (price=1) is the cost anchor.
        PRU should be 30 000.
        """
        mock_info.return_value = ("Bitcoin", Decimal("35000"))
        account = self._make_account(session, master_key)

        group = BinanceImportGroupPreview(
            group_index=0,
            timestamp="2024-01-01T12:00:00",
            rows=[
                BinanceImportRowPreview(
                    operation="Buy Crypto With Fiat", coin="BTC", change=0.1,
                    mapped_type="BUY", mapped_symbol="BTC", mapped_amount=0.1, mapped_price=0.0,
                ),
                BinanceImportRowPreview(
                    operation="Buy Crypto With Fiat", coin="EUR", change=-3000,
                    mapped_type="SPEND", mapped_symbol="EUR", mapped_amount=3000.0, mapped_price=1.0,
                ),
            ],
            summary="EUR → BTC",
            has_eur=True,
            needs_eur_input=False,
        )

        execute_import(session, "acc_pru", [group], master_key)
        summary = get_crypto_account_summary(session, account, master_key)

        btc = next(p for p in summary.positions if p.symbol == "BTC")
        assert btc.total_amount == Decimal("0.1")
        assert btc.total_invested == Decimal("3000.00")
        assert btc.average_buy_price == Decimal("30000.0000")

    @patch("services.crypto_transaction.get_crypto_info")
    def test_crypto_swap_with_anchor_pru(self, mock_info, session: Session, master_key: str):
        """
        Swap USDC→BTC with FIAT_ANCHOR 2760 EUR.
        PRU = 2760 / 0.1 = 27600.
        """
        mock_info.return_value = ("Bitcoin", Decimal("35000"))
        account = self._make_account(session, master_key, "acc_pru2")

        group = BinanceImportGroupPreview(
            group_index=0,
            timestamp="2024-01-01T12:00:00",
            rows=[
                BinanceImportRowPreview(
                    operation="Binance Convert", coin="USDC", change=-3000,
                    mapped_type="SPEND", mapped_symbol="USDC", mapped_amount=3000.0, mapped_price=0.0,
                ),
                BinanceImportRowPreview(
                    operation="Binance Convert", coin="BTC", change=0.1,
                    mapped_type="BUY", mapped_symbol="BTC", mapped_amount=0.1, mapped_price=0.0,
                ),
            ],
            summary="USDC → BTC",
            has_eur=False,
            needs_eur_input=True,
            eur_amount=2760.0,
        )

        execute_import(session, "acc_pru2", [group], master_key)
        summary = get_crypto_account_summary(session, account, master_key)

        btc = next(p for p in summary.positions if p.symbol == "BTC")
        assert btc.total_amount == Decimal("0.1")
        assert btc.total_invested == Decimal("2760.00")
        assert btc.average_buy_price == Decimal("27600.0000")

    @patch("services.crypto_transaction.get_crypto_info")
    def test_reward_has_zero_cost_basis(self, mock_info, session: Session, master_key: str):
        """Staking reward: REWARD rows have price=0 → total_invested=0."""
        mock_info.return_value = ("Ethereum", Decimal("3000"))
        account = self._make_account(session, master_key, "acc_pru3")

        group = BinanceImportGroupPreview(
            group_index=0,
            timestamp="2024-01-01T12:00:00",
            rows=[
                BinanceImportRowPreview(
                    operation="Crypto Box", coin="ETH", change=0.5,
                    mapped_type="REWARD", mapped_symbol="ETH", mapped_amount=0.5, mapped_price=0.0,
                ),
            ],
            summary="Reward",
            has_eur=False,
            needs_eur_input=False,
        )

        execute_import(session, "acc_pru3", [group], master_key)
        summary = get_crypto_account_summary(session, account, master_key)

        eth = next(p for p in summary.positions if p.symbol == "ETH")
        assert eth.total_amount == Decimal("0.5")
        assert eth.total_invested == Decimal("0.00")

    @patch("services.crypto_transaction.get_crypto_info")
    def test_sell_reduces_position_and_cost_basis(self, mock_info, session: Session, master_key: str):
        """
        Buy 1 BTC for 30000 EUR, then sell 0.5 BTC for 17500 EUR.
        After sell: total_amount=0.5, total_invested=15000 (50% of 30000).
        EUR should NOT create a BTC-like position.
        """
        mock_info.return_value = ("Bitcoin", Decimal("35000"))
        account = self._make_account(session, master_key, "acc_pru4")

        buy_group = BinanceImportGroupPreview(
            group_index=0, timestamp="2024-01-01T12:00:00",
            rows=[
                BinanceImportRowPreview(
                    operation="Buy Crypto With Fiat", coin="BTC", change=1.0,
                    mapped_type="BUY", mapped_symbol="BTC", mapped_amount=1.0, mapped_price=0.0,
                ),
                BinanceImportRowPreview(
                    operation="Buy Crypto With Fiat", coin="EUR", change=-30000,
                    mapped_type="SPEND", mapped_symbol="EUR", mapped_amount=30000.0, mapped_price=1.0,
                ),
            ],
            summary="EUR→BTC", has_eur=True, needs_eur_input=False,
        )
        sell_group = BinanceImportGroupPreview(
            group_index=1, timestamp="2024-02-01T12:00:00",
            rows=[
                BinanceImportRowPreview(
                    operation="Transaction Sold", coin="BTC", change=-0.5,
                    mapped_type="SPEND", mapped_symbol="BTC", mapped_amount=0.5, mapped_price=0.0,
                ),
                BinanceImportRowPreview(
                    operation="Transaction Revenue", coin="EUR", change=17500,
                    mapped_type="FIAT_DEPOSIT", mapped_symbol="EUR", mapped_amount=17500.0, mapped_price=1.0,
                ),
            ],
            summary="BTC→EUR", has_eur=True, needs_eur_input=False,
        )

        execute_import(session, "acc_pru4", [buy_group, sell_group], master_key)
        summary = get_crypto_account_summary(session, account, master_key)

        btc = next(p for p in summary.positions if p.symbol == "BTC")
        assert btc.total_amount == Decimal("0.5")
        assert btc.total_invested == Decimal("15000.00")

    @patch("services.crypto_transaction.get_crypto_info")
    def test_net_external_deposits_eur_buy(self, mock_info, session: Session, master_key: str):
        """
        Buy BTC with EUR: SPEND EUR is inside a crypto-buy group →
        net_external_deposits should NOT double-count the EUR as a withdrawal.
        Account-level total_invested = EUR deposited externally (0 in this case
        since euros entered via the exchange directly as a buy, not a wire).
        """
        mock_info.return_value = ("Bitcoin", Decimal("35000"))
        account = self._make_account(session, master_key, "acc_pru5")

        group = BinanceImportGroupPreview(
            group_index=0, timestamp="2024-01-01T12:00:00",
            rows=[
                BinanceImportRowPreview(
                    operation="Buy Crypto With Fiat", coin="BTC", change=0.1,
                    mapped_type="BUY", mapped_symbol="BTC", mapped_amount=0.1, mapped_price=0.0,
                ),
                BinanceImportRowPreview(
                    operation="Buy Crypto With Fiat", coin="EUR", change=-3000,
                    mapped_type="SPEND", mapped_symbol="EUR", mapped_amount=3000.0, mapped_price=1.0,
                ),
            ],
            summary="EUR→BTC", has_eur=True, needs_eur_input=False,
        )

        execute_import(session, "acc_pru5", [group], master_key)
        summary = get_crypto_account_summary(session, account, master_key)

        # SPEND EUR in a buy-group is excluded from net_external_deposits
        # So account total_invested = 0 (no standalone wire deposit recorded)
        assert summary.total_invested == Decimal("0.00")

    @patch("services.crypto_transaction.get_crypto_info")
    def test_fiat_deposit_counts_as_external(self, mock_info, session: Session, master_key: str):
        """
        Standalone EUR Deposit (wire transfer): FIAT_DEPOSIT EUR NOT in a
        trade group. Total invested = amount.
        """
        mock_info.return_value = ("Bitcoin", Decimal("35000"))
        account = self._make_account(session, master_key, "acc_pru6")

        group = BinanceImportGroupPreview(
            group_index=0, timestamp="2024-01-01T12:00:00",
            rows=[
                BinanceImportRowPreview(
                    operation="Deposit", coin="EUR", change=5000,
                    mapped_type="FIAT_DEPOSIT", mapped_symbol="EUR", mapped_amount=5000.0, mapped_price=1.0,
                ),
            ],
            summary="EUR Deposit", has_eur=True, needs_eur_input=False,
        )

        execute_import(session, "acc_pru6", [group], master_key)
        summary = get_crypto_account_summary(session, account, master_key)

        # Standalone EUR deposit should add to net_external_deposits
        assert summary.total_invested == Decimal("5000.00")
