"""Tests for the automatic bank balance sync via linked cashflows."""

from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlmodel import Session

from dtos.bank import BankAccountCreate, BankAccountUpdate
from dtos.cashflow import CashflowCreate, CashflowUpdate
from models.bank import BankAccount
from models.enums import BankAccountType, FlowType, Frequency
from services.bank import (
    create_bank_account,
    get_user_bank_accounts,
    update_bank_account,
)
from services.cashflow import create_cashflow
from services.encryption import decrypt_data


# ─── Helpers ─────────────────────────────────────────────────


def _make_account(session, master_key, user_uuid="sync_user", balance=Decimal("1000")) -> BankAccount:
    resp = create_bank_account(
        session,
        BankAccountCreate(name="Checking", balance=balance, account_type=BankAccountType.CHECKING),
        user_uuid,
        master_key,
    )
    return session.get(BankAccount, resp.id)


def _link_cashflow(session, master_key, account_id, amount, flow_type, frequency, transaction_date, user_uuid="sync_user"):
    return create_cashflow(
        session,
        CashflowCreate(
            name="Auto CF",
            flow_type=flow_type,
            category="test",
            amount=amount,
            frequency=frequency,
            transaction_date=transaction_date,
            bank_account_id=account_id,
        ),
        user_uuid,
        master_key,
    )


# ─── First connection ─────────────────────────────────────────


class TestFirstConnectionStamp:
    def test_stamps_today_on_first_fetch(self, session: Session, master_key: str):
        user_uuid = "first_stamp_user"
        acc = _make_account(session, master_key, user_uuid=user_uuid)
        assert acc.balance_updated_at is None

        with patch("services.bank.date") as mock_date:
            today = date(2026, 3, 21)
            mock_date.today.return_value = today
            get_user_bank_accounts(session, user_uuid, master_key)

        session.refresh(acc)
        assert acc.balance_updated_at == today

    def test_does_not_change_balance_on_first_fetch(self, session: Session, master_key: str):
        user_uuid = "first_no_change_user"
        acc = _make_account(session, master_key, user_uuid=user_uuid, balance=Decimal("2000"))

        # Link an outflow that should theoretically fire
        _link_cashflow(
            session, master_key, acc.uuid,
            amount=Decimal("500"),
            flow_type=FlowType.OUTFLOW,
            frequency=Frequency.MONTHLY,
            transaction_date=date(2026, 2, 5),
            user_uuid=user_uuid,
        )

        with patch("services.bank.date") as mock_date:
            mock_date.today.return_value = date(2026, 3, 21)
            summary = get_user_bank_accounts(session, user_uuid, master_key)

        assert summary.accounts[0].balance == Decimal("2000")


# ─── Subsequent sync ─────────────────────────────────────────


class TestAutoSync:
    def test_outflow_subtracted(self, session: Session, master_key: str):
        """A monthly outflow since last sync should be removed from balance."""
        user_uuid = "outflow_sync_user"
        acc = _make_account(session, master_key, user_uuid=user_uuid, balance=Decimal("1000"))

        # Simulate last sync was Feb 1 — stamp it directly
        acc.balance_updated_at = date(2026, 2, 1)
        session.add(acc)
        session.commit()

        # Outflow of 500 on the 10th of each month
        _link_cashflow(
            session, master_key, acc.uuid,
            amount=Decimal("500"),
            flow_type=FlowType.OUTFLOW,
            frequency=Frequency.MONTHLY,
            transaction_date=date(2026, 1, 10),
            user_uuid=user_uuid,
        )

        with patch("services.bank.date") as mock_date:
            mock_date.today.return_value = date(2026, 3, 21)
            summary = get_user_bank_accounts(session, user_uuid, master_key)

        # Feb 10 and Mar 10 both fired → 1000 - 500 - 500 = 0
        assert summary.accounts[0].balance == Decimal("0")

    def test_inflow_added(self, session: Session, master_key: str):
        """A monthly inflow since last sync should be added to balance."""
        user_uuid = "inflow_sync_user"
        acc = _make_account(session, master_key, user_uuid=user_uuid, balance=Decimal("0"))

        acc.balance_updated_at = date(2026, 2, 27)
        session.add(acc)
        session.commit()

        _link_cashflow(
            session, master_key, acc.uuid,
            amount=Decimal("2000"),
            flow_type=FlowType.INFLOW,
            frequency=Frequency.MONTHLY,
            transaction_date=date(2026, 1, 28),
            user_uuid=user_uuid,
        )

        with patch("services.bank.date") as mock_date:
            mock_date.today.return_value = date(2026, 3, 21)
            summary = get_user_bank_accounts(session, user_uuid, master_key)

        # Feb 28 and Mar 28: but Mar 28 > Mar 21 → only Feb 28 fires → 0 + 2000 = 2000
        assert summary.accounts[0].balance == Decimal("2000")

    def test_multiple_cashflows_net_delta(self, session: Session, master_key: str):
        """Multiple linked cashflows are all applied in the same pass."""
        user_uuid = "multi_cf_user"
        acc = _make_account(session, master_key, user_uuid=user_uuid, balance=Decimal("500"))

        acc.balance_updated_at = date(2026, 2, 28)
        session.add(acc)
        session.commit()

        # +3000 salary on 1st
        _link_cashflow(session, master_key, acc.uuid, Decimal("3000"), FlowType.INFLOW,
                       Frequency.MONTHLY, date(2026, 1, 1), user_uuid=user_uuid)
        # -1200 rent on 5th
        _link_cashflow(session, master_key, acc.uuid, Decimal("1200"), FlowType.OUTFLOW,
                       Frequency.MONTHLY, date(2026, 1, 5), user_uuid=user_uuid)
        # -50 subscription on 15th
        _link_cashflow(session, master_key, acc.uuid, Decimal("50"), FlowType.OUTFLOW,
                       Frequency.MONTHLY, date(2026, 1, 15), user_uuid=user_uuid)

        with patch("services.bank.date") as mock_date:
            mock_date.today.return_value = date(2026, 3, 21)
            summary = get_user_bank_accounts(session, user_uuid, master_key)

        # Mar 1: +3000, Mar 5: -1200, Mar 15: -50 → net +1750 → 500 + 1750 = 2250
        assert summary.accounts[0].balance == Decimal("2250")

    def test_no_linked_cashflows_just_stamps(self, session: Session, master_key: str):
        """Account with no linked cashflows just advances the sync date."""
        user_uuid = "no_cf_user"
        acc = _make_account(session, master_key, user_uuid=user_uuid, balance=Decimal("999"))

        acc.balance_updated_at = date(2026, 1, 1)
        session.add(acc)
        session.commit()

        with patch("services.bank.date") as mock_date:
            today = date(2026, 3, 21)
            mock_date.today.return_value = today
            summary = get_user_bank_accounts(session, user_uuid, master_key)

        assert summary.accounts[0].balance == Decimal("999")
        session.refresh(acc)
        assert acc.balance_updated_at == today

    def test_already_up_to_date_no_double_apply(self, session: Session, master_key: str):
        """If balance_updated_at == today, no cashflows are re-applied."""
        user_uuid = "no_double_user"
        acc = _make_account(session, master_key, user_uuid=user_uuid, balance=Decimal("1000"))

        today = date(2026, 3, 21)
        acc.balance_updated_at = today
        session.add(acc)
        session.commit()

        _link_cashflow(session, master_key, acc.uuid, Decimal("500"), FlowType.OUTFLOW,
                       Frequency.MONTHLY, date(2026, 3, 5), user_uuid=user_uuid)

        with patch("services.bank.date") as mock_date:
            mock_date.today.return_value = today
            summary = get_user_bank_accounts(session, user_uuid, master_key)

        # Already synced today → no change
        assert summary.accounts[0].balance == Decimal("1000")

    def test_unlinked_cashflow_not_applied(self, session: Session, master_key: str):
        """A cashflow with no bank_account_id must not affect any account."""
        user_uuid = "unlinked_cf_user"
        acc = _make_account(session, master_key, user_uuid=user_uuid, balance=Decimal("1000"))

        acc.balance_updated_at = date(2026, 2, 1)
        session.add(acc)
        session.commit()

        # Cashflow with no bank link
        create_cashflow(
            session,
            CashflowCreate(
                name="Freelance",
                flow_type=FlowType.INFLOW,
                category="work",
                amount=Decimal("500"),
                frequency=Frequency.MONTHLY,
                transaction_date=date(2026, 1, 10),
                bank_account_id=None,
            ),
            user_uuid,
            master_key,
        )

        with patch("services.bank.date") as mock_date:
            mock_date.today.return_value = date(2026, 3, 21)
            summary = get_user_bank_accounts(session, user_uuid, master_key)

        assert summary.accounts[0].balance == Decimal("1000")


# ─── Manual balance update resets sync date ───────────────────


class TestManualBalanceReset:
    def test_manual_update_resets_balance_updated_at(self, session: Session, master_key: str):
        user_uuid = "manual_reset_user"
        acc_resp = create_bank_account(
            session,
            BankAccountCreate(name="Reset", balance=Decimal("500"), account_type=BankAccountType.CHECKING),
            user_uuid,
            master_key,
        )
        acc = session.get(BankAccount, acc_resp.id)
        acc.balance_updated_at = date(2026, 1, 15)
        session.add(acc)
        session.commit()

        with patch("services.bank.date") as mock_date:
            today = date(2026, 3, 21)
            mock_date.today.return_value = today
            update_bank_account(session, acc, BankAccountUpdate(balance=Decimal("1200")), master_key)

        session.refresh(acc)
        assert acc.balance_updated_at == today
