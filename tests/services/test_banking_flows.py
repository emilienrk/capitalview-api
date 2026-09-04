"""
Observed-flow aggregation (services/banking/flows.py).

Rows are written through `store_transactions`, never hand-built: the aggregation
reads what the sync actually persists, and a hand-built row could disagree with
it silently.
"""
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlmodel import Session

from models.banking import BankAccountLink, BankSession
from services.banking.flows import _months_back, compute_real_flows
from services.banking.transactions import store_transactions
from services.encryption import encrypt_data, hash_index

USER = "flows_user"
ACCOUNT_A = "account-a"
ACCOUNT_B = "account-b"


def _raw(amount: str, direction: str, day: str, *, ref: str, status: str = "BOOK",
         currency: str = "EUR") -> dict:
    return {
        "entry_reference": ref,
        "transaction_amount": {"currency": currency, "amount": amount},
        "credit_debit_indicator": direction,
        "status": status,
        "booking_date": day,
        "value_date": day,
        "transaction_date": day,
        "remittance_information": ["peu importe"],
    }


def _bank_session(session: Session, master_key: str) -> str:
    """The link's foreign key: one consent both accounts hang off."""
    existing = session.get(BankSession, "sess-flows")
    if existing is not None:
        return existing.uuid
    row = BankSession(
        uuid="sess-flows",
        user_uuid_bidx=hash_index(USER, master_key),
        session_id_enc=encrypt_data("eb-sess", master_key),
        status="AUTHORIZED",
        consent_valid_until=datetime(2027, 1, 1, tzinfo=timezone.utc),
        authorized_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    session.add(row)
    session.commit()
    return row.uuid


def _link(session: Session, master_key: str, account_uuid: str) -> None:
    """Only linked accounts feed the aggregation."""
    _bank_session(session, master_key)
    session.add(
        BankAccountLink(
            user_uuid_bidx=hash_index(USER, master_key),
            bank_account_uuid_bidx=hash_index(account_uuid, master_key),
            session_uuid="sess-flows",
            identification_hash_bidx=hash_index(f"ih-{account_uuid}", master_key),
            account_uid_enc=encrypt_data("uid", master_key),
            anchor_date=date(2026, 1, 1),
            anchor_balance_enc=encrypt_data("0", master_key),
            last_synced_at=date(2026, 1, 1),
        )
    )
    session.commit()


def _store(session: Session, master_key: str, account_uuid: str, *raws) -> None:
    store_transactions(session, master_key, account_uuid, list(raws))


class TestWindow:
    def test_months_back_ends_on_the_anchor_month(self):
        assert _months_back(date(2026, 3, 15), 4) == ["2025-12", "2026-01", "2026-02", "2026-03"]

    def test_months_back_crosses_the_year_boundary(self):
        assert _months_back(date(2026, 1, 5), 2) == ["2025-12", "2026-01"]

    def test_no_linked_account_is_zeroes_not_an_error(self, session: Session, master_key: str):
        result = compute_real_flows(session, USER, master_key, months=3, today=date(2026, 3, 10))
        assert result.account_count == 0
        assert result.inflow == Decimal("0")
        assert [m.period for m in result.months] == ["2026-01", "2026-02", "2026-03"]

    def test_a_movement_outside_the_window_is_left_out(self, session: Session, master_key: str):
        _link(session, master_key, ACCOUNT_A)
        _store(
            session, master_key, ACCOUNT_A,
            _raw("100.00", "CRDT", "2026-03-05", ref="inside"),
            _raw("999.00", "CRDT", "2025-09-05", ref="outside"),
        )
        result = compute_real_flows(session, USER, master_key, months=3, today=date(2026, 3, 10))
        assert result.inflow == Decimal("100.00")


class TestDirection:
    def test_credits_and_debits_land_on_their_own_side(self, session: Session, master_key: str):
        _link(session, master_key, ACCOUNT_A)
        _store(
            session, master_key, ACCOUNT_A,
            _raw("2000.00", "CRDT", "2026-03-01", ref="salary"),
            _raw("850.00", "DBIT", "2026-03-03", ref="rent"),
            _raw("12.50", "DBIT", "2026-03-04", ref="coffee"),
        )
        result = compute_real_flows(session, USER, master_key, months=1, today=date(2026, 3, 10))
        assert result.inflow == Decimal("2000.00")
        assert result.outflow == Decimal("862.50")
        assert result.net == Decimal("1137.50")
        [month] = result.months
        assert month.inflow_count == 1
        assert month.outflow_count == 2

    def test_a_pending_movement_stays_out_of_the_monthly_figures(
        self, session: Session, master_key: str
    ):
        """It is not final: folding it in would move a total that then moves again."""
        _link(session, master_key, ACCOUNT_A)
        _store(
            session, master_key, ACCOUNT_A,
            _raw("100.00", "DBIT", "2026-03-01", ref="booked"),
            _raw("30.00", "DBIT", "2026-03-02", ref="pending", status="PDNG"),
        )
        result = compute_real_flows(session, USER, master_key, months=1, today=date(2026, 3, 10))
        assert result.outflow == Decimal("100.00")
        assert result.pending_count == 1
        assert result.pending_outflow == Decimal("30.00")


class TestInternalTransfers:
    """Moving money between one's own accounts is neither income nor spending."""

    def _both_sides(self, session: Session, master_key: str, day_a: str, day_b: str) -> None:
        _link(session, master_key, ACCOUNT_A)
        _link(session, master_key, ACCOUNT_B)
        _store(session, master_key, ACCOUNT_A, _raw("500.00", "DBIT", day_a, ref="out-leg"))
        _store(session, master_key, ACCOUNT_B, _raw("500.00", "CRDT", day_b, ref="in-leg"))

    def test_both_legs_are_excluded(self, session: Session, master_key: str):
        self._both_sides(session, master_key, "2026-03-05", "2026-03-05")
        result = compute_real_flows(session, USER, master_key, months=1, today=date(2026, 3, 10))
        assert result.inflow == Decimal("0")
        assert result.outflow == Decimal("0")
        assert result.internal_transfers_excluded == 1
        assert result.internal_transfers_amount == Decimal("500.00")

    def test_a_weekend_between_the_legs_still_pairs(self, session: Session, master_key: str):
        self._both_sides(session, master_key, "2026-03-06", "2026-03-09")
        result = compute_real_flows(session, USER, master_key, months=1, today=date(2026, 3, 10))
        assert result.internal_transfers_excluded == 1

    def test_legs_too_far_apart_are_two_real_movements(self, session: Session, master_key: str):
        self._both_sides(session, master_key, "2026-03-01", "2026-03-20")
        result = compute_real_flows(session, USER, master_key, months=1, today=date(2026, 3, 25))
        assert result.internal_transfers_excluded == 0
        assert result.inflow == Decimal("500.00")
        assert result.outflow == Decimal("500.00")

    def test_the_user_can_switch_the_exclusion_off(self, session: Session, master_key: str):
        self._both_sides(session, master_key, "2026-03-05", "2026-03-05")
        result = compute_real_flows(
            session, USER, master_key, months=1,
            exclude_internal_transfers=False, today=date(2026, 3, 10),
        )
        assert result.inflow == Decimal("500.00")
        assert result.outflow == Decimal("500.00")
        assert result.internal_transfers_excluded == 0

    def test_one_credit_cannot_settle_two_debits(self, session: Session, master_key: str):
        """Otherwise a single incoming transfer would erase every same-sized
        debit of the month, and real spending would vanish."""
        _link(session, master_key, ACCOUNT_A)
        _link(session, master_key, ACCOUNT_B)
        _store(
            session, master_key, ACCOUNT_A,
            _raw("40.00", "DBIT", "2026-03-05", ref="d1"),
            _raw("40.00", "DBIT", "2026-03-06", ref="d2"),
        )
        _store(session, master_key, ACCOUNT_B, _raw("40.00", "CRDT", "2026-03-05", ref="c1"))
        result = compute_real_flows(session, USER, master_key, months=1, today=date(2026, 3, 10))
        assert result.internal_transfers_excluded == 1
        assert result.outflow == Decimal("40.00")

    def test_a_same_account_pair_is_not_a_transfer(self, session: Session, master_key: str):
        """A refund landing on the account it was spent from is real, both ways."""
        _link(session, master_key, ACCOUNT_A)
        _store(
            session, master_key, ACCOUNT_A,
            _raw("60.00", "DBIT", "2026-03-05", ref="purchase"),
            _raw("60.00", "CRDT", "2026-03-06", ref="refund"),
        )
        result = compute_real_flows(session, USER, master_key, months=1, today=date(2026, 3, 10))
        assert result.internal_transfers_excluded == 0
        assert result.inflow == Decimal("60.00")
        assert result.outflow == Decimal("60.00")


class TestSeveralAccounts:
    """A current account, a second current account and a savings account: the
    total spans all of them, and only genuine transfers between them drop out."""

    def _three(self, session: Session, master_key: str) -> None:
        for name in (ACCOUNT_A, ACCOUNT_B, "savings"):
            _link(session, master_key, name)

    def test_the_total_spans_every_linked_account(self, session: Session, master_key: str):
        self._three(session, master_key)
        _store(session, master_key, ACCOUNT_A, _raw("1000.00", "CRDT", "2026-03-01", ref="pay"))
        _store(session, master_key, ACCOUNT_B, _raw("30.00", "DBIT", "2026-03-02", ref="shop"))
        _store(session, master_key, "savings", _raw("2.50", "CRDT", "2026-03-31", ref="interest"))

        result = compute_real_flows(session, USER, master_key, months=1, today=date(2026, 3, 31))
        assert result.account_count == 3
        assert result.inflow == Decimal("1002.50")
        assert result.outflow == Decimal("30.00")

    def test_a_move_to_savings_is_neither_income_nor_spending(
        self, session: Session, master_key: str
    ):
        self._three(session, master_key)
        _store(session, master_key, ACCOUNT_A, _raw("400.00", "DBIT", "2026-03-10", ref="to-savings"))
        _store(session, master_key, "savings", _raw("400.00", "CRDT", "2026-03-10", ref="from-current"))

        result = compute_real_flows(session, USER, master_key, months=1, today=date(2026, 3, 31))
        assert result.internal_transfers_excluded == 1
        assert result.inflow == Decimal("0")
        assert result.outflow == Decimal("0")

    def test_the_nearest_leg_wins_over_the_earliest_one(
        self, session: Session, master_key: str
    ):
        """Two credits sit inside the tolerance, in two different months. Pairing
        the earlier one would leave the same-day pair unmatched — and move a
        month's worth of income to the wrong month.

        The two candidates are told apart by the month the surviving credit
        lands in, which is the only place the choice shows.
        """
        self._three(session, master_key)
        _store(session, master_key, ACCOUNT_A, _raw("250.00", "CRDT", "2026-02-27", ref="earlier"))
        _store(session, master_key, "savings", _raw("250.00", "CRDT", "2026-03-01", ref="same-day"))
        _store(session, master_key, ACCOUNT_B, _raw("250.00", "DBIT", "2026-03-01", ref="debit"))

        result = compute_real_flows(session, USER, master_key, months=2, today=date(2026, 3, 31))
        assert result.internal_transfers_excluded == 1
        by_period = {m.period: m for m in result.months}
        # The same-day credit was the transfer's other leg; February's is real.
        assert by_period["2026-02"].inflow == Decimal("250.00")
        assert by_period["2026-03"].inflow == Decimal("0")

    def test_linked_account_names_are_reported(self, session: Session, master_key: str):
        from dtos.bank import BankAccountCreate
        from models.enums import BankAccountType
        from services.bank import create_bank_account

        created = create_bank_account(
            session,
            BankAccountCreate(name="Livret A", balance="500.00",
                              account_type=BankAccountType.LIVRET_A),
            USER, master_key,
        )
        _link(session, master_key, created.id)
        result = compute_real_flows(session, USER, master_key, months=1, today=date(2026, 3, 31))
        assert result.account_names == ["Livret A"]


class TestCurrency:
    def test_a_foreign_amount_never_joins_the_main_total(
        self, session: Session, master_key: str
    ):
        """It arrives unconverted, with no exchange rate: adding it would invent
        a number."""
        _link(session, master_key, ACCOUNT_A)
        _store(
            session, master_key, ACCOUNT_A,
            _raw("100.00", "DBIT", "2026-03-01", ref="eur1"),
            _raw("200.00", "DBIT", "2026-03-02", ref="eur2"),
            _raw("12.63", "DBIT", "2026-03-03", ref="chf", currency="CHF"),
        )
        result = compute_real_flows(session, USER, master_key, months=1, today=date(2026, 3, 10))
        assert result.currency == "EUR"
        assert result.outflow == Decimal("300.00")
        assert [(c.currency, c.outflow) for c in result.other_currencies] == [("CHF", Decimal("12.63"))]


class TestAverage:
    def test_the_average_uses_the_months_that_carry_data(
        self, session: Session, master_key: str
    ):
        """Dividing a two-month history by twelve reads as a collapse in income."""
        _link(session, master_key, ACCOUNT_A)
        _store(
            session, master_key, ACCOUNT_A,
            _raw("1000.00", "CRDT", "2026-02-10", ref="feb"),
            _raw("1000.00", "CRDT", "2026-03-10", ref="mar"),
        )
        result = compute_real_flows(session, USER, master_key, months=12, today=date(2026, 3, 20))
        assert result.covered_months == 2
        assert result.monthly_inflow == Decimal("1000.00")


class TestImportedAccount:
    """An account no bank API reaches gets its movements from a CSV import.

    That is the whole point for a Livret A: without its side of the transfer,
    the debit leaving the current account has no credit to pair with, and every
    euro moved to savings reads as spending.
    """

    def _livret(self, session: Session, master_key: str) -> str:
        from dtos.bank import BankAccountCreate
        from models.enums import BankAccountType
        from services.bank import create_bank_account

        return create_bank_account(
            session,
            BankAccountCreate(name="Livret A", balance="0",
                              account_type=BankAccountType.LIVRET_A),
            USER, master_key,
        ).id

    def test_an_imported_movement_counts_like_a_synced_one(
        self, session: Session, master_key: str
    ):
        livret = self._livret(session, master_key)
        _store(session, master_key, livret, _raw("2.50", "CRDT", "2026-03-31", ref="interest"))

        result = compute_real_flows(session, USER, master_key, months=1, today=date(2026, 3, 31))
        assert result.account_count == 1
        assert result.inflow == Decimal("2.50")

    def test_a_move_to_an_imported_savings_account_is_not_spending(
        self, session: Session, master_key: str
    ):
        _link(session, master_key, ACCOUNT_A)
        livret = self._livret(session, master_key)
        _store(session, master_key, ACCOUNT_A, _raw("400.00", "DBIT", "2026-03-10", ref="out"))
        _store(session, master_key, livret, _raw("400.00", "CRDT", "2026-03-10", ref="in"))

        result = compute_real_flows(session, USER, master_key, months=1, today=date(2026, 3, 31))
        assert result.internal_transfers_excluded == 1
        assert result.internal_transfers_amount == Decimal("400.00")
        assert result.outflow == Decimal("0")

    def test_an_account_nobody_imported_anything_into_stays_out(
        self, session: Session, master_key: str
    ):
        """It would name a total it contributes nothing to."""
        _link(session, master_key, ACCOUNT_A)
        self._livret(session, master_key)
        _store(session, master_key, ACCOUNT_A, _raw("30.00", "DBIT", "2026-03-02", ref="shop"))

        result = compute_real_flows(session, USER, master_key, months=1, today=date(2026, 3, 31))
        assert result.account_count == 1
