"""
Tests for the sync sequence, the anchors, the reconciliation check and the
date-bounded history replacement (spec §D).

No real network: `build_client` is always replaced by a double, as in
tests/routes/test_banking_linking.py. Dates are all relative to the real
`date.today()` — the sequence reasons in days, and freezing time here would
hide the "never past yesterday" convention rather than exercise it.
"""

import json
from unittest.mock import patch
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from types import SimpleNamespace
from sqlmodel import Session, select

from dtos.bank import BankAccountCreate
from dtos.banking import BankConnectionUpdate
from models.account_history import AccountHistory
from models.bank import BankAccount
from models.banking import BankAccountLink, BankSession, BankTransaction
from models.enums import AccountCategory, BankAccountType
from services.bank import create_bank_account, get_user_bank_accounts
from services.banking.credentials import upsert_connection
from services.banking.errors import InvalidPeriodError, SessionInvalidError
from services.banking.sync import (
    SEED_DATE_FROM,
    AccountingBalanceUnavailableError,
    _accounting_balance,
    sync_user_accounts,
)
from services.banking.transactions import store_transactions
from services.encryption import decrypt_data, encrypt_data, hash_index

USER = "sync-user"

TODAY = date.today()
YESTERDAY = TODAY - timedelta(days=1)


# ---------------------------------------------------------------------------
# Doubles and fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sqlite_pg_insert(monkeypatch):
    """Replace pg_insert (PostgreSQL-specific) with a plain SA insert for SQLite
    tests — same workaround as tests/services/test_bank.py:30."""
    import sqlalchemy as sa

    def _fake(table):
        class _Stmt:
            def values(self, rows):
                self._rows = rows
                return self

            def on_conflict_do_nothing(self, **kwargs):
                return sa.insert(table).values(self._rows)

        return _Stmt()

    monkeypatch.setattr("services.bank.pg_insert", _fake)


class FakeClient:
    """Records every call and returns canned payloads. Never touches the network."""

    def __init__(self, balances: dict, feeds: dict, period_error: dict | None = None):
        self.balances = balances
        self.feeds = feeds
        self.period_error = period_error or {}
        self.balance_calls: list[str] = []
        self.transaction_calls: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def close(self):
        pass

    def get_balances(self, uid):
        self.balance_calls.append(uid)
        payload = self.balances[uid]
        if isinstance(payload, Exception):
            raise payload
        return payload

    def iter_transactions(self, uid, date_from=None, strategy="default"):
        self.transaction_calls.append((uid, date_from, strategy))
        error = self.period_error.pop(uid, None)

        def _walk():
            if error is not None:
                raise error
            feed = self.feeds.get(uid, [])
            if isinstance(feed, Exception):
                raise feed
            yield from feed

        return _walk()


def _balances(accounting: str, real_time: str | None = "0.00", currency: str = "EUR") -> dict:
    """Two balances coexist (§F). The real-time one is listed *first* on purpose:
    taking the first element of the list is exactly the documented mistake."""
    balances = []
    if real_time is not None:
        balances.append({
            "name": "Instant balance",
            "balance_amount": {"currency": currency, "amount": real_time},
            "balance_type": "XPCD",
        })
    balances.append({
        "name": "Booked balance",
        "balance_amount": {"currency": currency, "amount": accounting},
        "balance_type": "CLBD",
    })
    return {"balances": balances}


def _raw(amount, day, direction="DBIT", status="BOOK", currency="EUR", ref=None, **extra):
    """A transaction shaped like the captured Boursorama payloads. A pending
    operation carries no booking_date, as in the real capture."""
    booked = status == "BOOK"
    raw = {
        "entry_reference": ref,
        "transaction_amount": {"currency": currency, "amount": str(amount)},
        "credit_debit_indicator": direction,
        "status": status,
        "booking_date": day.isoformat() if booked else None,
        "value_date": None,
        "transaction_date": day.isoformat(),
        "remittance_information": ["PRLV TEST"],
        "transaction_id": None,
    }
    raw.update(extra)
    return raw


def _credentials(session: Session, master_key: str, user_uuid: str = USER) -> None:
    upsert_connection(
        session,
        user_uuid,
        master_key,
        BankConnectionUpdate(application_id="app-1", private_key="key-1"),
    )


def _bank_session(
    session: Session, master_key: str, accounts: list[dict], user_uuid: str = USER
) -> BankSession:
    row = BankSession(
        user_uuid_bidx=hash_index(user_uuid, master_key),
        session_id_enc=encrypt_data("sess-1", master_key),
        aspsp_name_enc=encrypt_data("Boursorama", master_key),
        aspsp_country_enc=encrypt_data("FR", master_key),
        status="AUTHORIZED",
        consent_valid_until=datetime.now(timezone.utc) + timedelta(days=180),
        authorized_at=datetime.now(timezone.utc),
        accounts_enc=encrypt_data(json.dumps(accounts), master_key),
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _account(
    session: Session,
    master_key: str,
    name: str = "Compte courant",
    balance: Decimal = Decimal("1000"),
    user_uuid: str = USER,
    currency: str = "EUR",
) -> BankAccount:
    with patch("services.bank.has_exchange_rate", return_value=True):
        resp = create_bank_account(
            session,
            BankAccountCreate(
                name=name,
                balance=balance,
                account_type=BankAccountType.CHECKING,
                currency=currency,
            ),
            user_uuid,
            master_key,
        )
    return session.get(BankAccount, resp.id)


def _link(
    session: Session,
    master_key: str,
    bank_session: BankSession,
    account: BankAccount,
    identification_hash: str,
    uid: str,
    anchor_date: date,
    anchor_balance: Decimal,
    last_synced_at: date | None = None,
    user_uuid: str = USER,
) -> BankAccountLink:
    """A link. `last_synced_at` defaults to the never-synced marker the
    rattachement step writes (anchor_date - 1 day)."""
    link = BankAccountLink(
        user_uuid_bidx=hash_index(user_uuid, master_key),
        bank_account_uuid_bidx=hash_index(account.uuid, master_key),
        session_uuid=bank_session.uuid,
        identification_hash_bidx=hash_index(identification_hash, master_key),
        account_uid_enc=encrypt_data(uid, master_key),
        anchor_date=anchor_date,
        anchor_balance_enc=encrypt_data(str(anchor_balance), master_key),
        last_synced_at=last_synced_at if last_synced_at is not None else anchor_date - timedelta(days=1),
    )
    session.add(link)
    session.commit()
    session.refresh(link)
    return link


def _install(monkeypatch, client: FakeClient) -> None:
    monkeypatch.setattr("services.banking.sync.build_client", lambda *a, **kw: client)


def _one_account_setup(
    session: Session,
    master_key: str,
    accounting: str,
    feed: list,
    anchor_date: date,
    anchor_balance: Decimal,
    last_synced_at: date | None = None,
    real_time: str | None = "0.00",
    cash_account_type: str = "CACC",
    currency: str = "EUR",
):
    """The common case: one linked current account, already seeded unless told
    otherwise (last_synced_at >= anchor_date is the seeded marker)."""
    _credentials(session, master_key)
    bank_session = _bank_session(
        session,
        master_key,
        [{
            "uid": "uid-current",
            "identification_hash": "ih-current",
            "cash_account_type": cash_account_type,
        }],
    )
    account = _account(session, master_key, currency=currency)
    link = _link(
        session,
        master_key,
        bank_session,
        account,
        "ih-current",
        "uid-current",
        anchor_date,
        anchor_balance,
        last_synced_at if last_synced_at is not None else anchor_date,
    )
    client = FakeClient(
        balances={"uid-current": _balances(accounting, real_time, currency)},
        feeds={"uid-current": feed},
    )
    return account, link, client


def _history(session: Session, account: BankAccount, master_key: str) -> list[AccountHistory]:
    return session.exec(
        select(AccountHistory)
        .where(AccountHistory.account_id_bidx == hash_index(account.uuid, master_key))
        .order_by(AccountHistory.snapshot_date)
    ).all()


def _value_on(rows: list[AccountHistory], day: date, master_key: str) -> Decimal:
    for row in rows:
        if row.snapshot_date == day:
            return Decimal(decrypt_data(row.total_value_enc, master_key))
    raise KeyError(f"no snapshot for {day}")


def _manual_snapshot(
    session: Session, account: BankAccount, master_key: str, day: date, value: str
) -> AccountHistory:
    row = AccountHistory(
        user_uuid_bidx=account.user_uuid_bidx,
        account_id_bidx=hash_index(account.uuid, master_key),
        account_type=AccountCategory.BANK.value,
        snapshot_date=day,
        total_value_enc=encrypt_data(value, master_key),
        total_invested_enc=encrypt_data(value, master_key),
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


# ---------------------------------------------------------------------------
# D1 — triggering and the daily cap
# ---------------------------------------------------------------------------


class TestDailyCap:
    def test_second_sync_the_same_day_is_a_no_op(
        self, session: Session, master_key: str, monkeypatch, sqlite_pg_insert
    ):
        account, link, client = _one_account_setup(
            session,
            master_key,
            accounting="930.00",
            feed=[_raw("100", TODAY - timedelta(days=5), ref="r1")],
            anchor_date=TODAY - timedelta(days=10),
            anchor_balance=Decimal("1030"),
        )
        _install(monkeypatch, client)

        first = sync_user_accounts(session, USER, master_key)
        assert [r.status for r in first] == ["synced"]

        second = sync_user_accounts(session, USER, master_key)

        # Not an error, and no second call to the bank.
        assert [r.status for r in second] == ["skipped_daily_cap"]
        assert client.balance_calls == ["uid-current"]
        assert len(client.transaction_calls) == 1

    def test_a_sync_the_next_day_runs_again(
        self, session: Session, master_key: str, monkeypatch, sqlite_pg_insert
    ):
        account, link, client = _one_account_setup(
            session,
            master_key,
            accounting="930.00",
            feed=[],
            anchor_date=TODAY - timedelta(days=10),
            anchor_balance=Decimal("930"),
        )
        _install(monkeypatch, client)
        link.last_synced_at = YESTERDAY
        session.add(link)
        session.commit()

        results = sync_user_accounts(session, USER, master_key)
        assert [r.status for r in results] == ["synced"]


# ---------------------------------------------------------------------------
# D2 / F — the balance that is authoritative, and the fetch window
# ---------------------------------------------------------------------------


class TestBalanceSelection:
    def test_accounting_balance_is_retained_never_the_real_time_one(
        self, session: Session, master_key: str, monkeypatch, sqlite_pg_insert
    ):
        account, link, client = _one_account_setup(
            session,
            master_key,
            accounting="406.70",
            real_time="244.07",
            feed=[],
            anchor_date=TODAY - timedelta(days=3),
            anchor_balance=Decimal("406.70"),
        )
        _install(monkeypatch, client)

        sync_user_accounts(session, USER, master_key)

        session.refresh(link)
        session.refresh(account)
        assert Decimal(decrypt_data(link.anchor_balance_enc, master_key)) == Decimal("406.70")
        assert Decimal(decrypt_data(account.balance_enc, master_key)) == Decimal("406.70")

    def test_a_feed_without_an_accounting_balance_is_an_error_not_a_guess(
        self, session: Session, master_key: str, monkeypatch, sqlite_pg_insert
    ):
        account, link, client = _one_account_setup(
            session,
            master_key,
            accounting="0",
            feed=[],
            anchor_date=TODAY - timedelta(days=3),
            anchor_balance=Decimal("100"),
        )
        client.balances["uid-current"] = {
            "balances": [
                {
                    "name": "Instant balance",
                    "balance_amount": {"currency": "EUR", "amount": "244.07"},
                    "balance_type": "XPCD",
                }
            ]
        }
        _install(monkeypatch, client)

        results = sync_user_accounts(session, USER, master_key)

        assert [r.status for r in results] == ["error"]
        session.refresh(link)
        assert link.anchor_date == TODAY - timedelta(days=3)

    def test_the_card_fallback_is_othr_only_and_never_the_real_time_balance(self):
        """Ruling R19 needs *a* balance on a card account — the real capture
        publishes one single OTHR there and no CLBD at all. It does not license
        "any EUR balance": XPCD is the real-time balance, and folding pending
        operations into an anchor is the substitution
        `AccountingBalanceUnavailableError` exists to forbid (§F)."""
        othr_only = {
            "balances": [
                {"balance_amount": {"currency": "EUR", "amount": "0"}, "balance_type": "OTHR"}
            ]
        }
        assert _accounting_balance(othr_only, "EUR", is_card=True) == Decimal("0")

        real_time_only = {
            "balances": [
                {"balance_amount": {"currency": "EUR", "amount": "244.07"}, "balance_type": "XPCD"}
            ]
        }
        with pytest.raises(AccountingBalanceUnavailableError):
            _accounting_balance(real_time_only, "EUR", is_card=True)

    def test_a_regular_account_never_falls_back_at_all(self):
        othr_only = {
            "balances": [
                {"balance_amount": {"currency": "EUR", "amount": "0"}, "balance_type": "OTHR"}
            ]
        }
        with pytest.raises(AccountingBalanceUnavailableError):
            _accounting_balance(othr_only, "EUR")

    def test_the_refusal_names_what_the_bank_did_publish(self):
        """The message reaches the user's screen through `BankAccountSyncResult
        .detail`, and it is the only place the ASPSP's actual balance types are
        ever visible: which ones a bank publishes is in no contract."""
        payload = {
            "balances": [
                {"balance_amount": {"currency": "EUR", "amount": "3000"}, "balance_type": "ITAV"},
                {"balance_amount": {"currency": "CHF", "amount": "12.63"}, "balance_type": "CLBD"},
            ]
        }
        with pytest.raises(AccountingBalanceUnavailableError) as excinfo:
            _accounting_balance(payload, "EUR")

        message = str(excinfo.value)
        assert "ITAV/EUR" in message
        assert "CLBD/CHF" in message
        # No amount: this string is displayed, and a balance is not an error detail.
        assert "3000" not in message

    def test_the_refusal_says_so_when_the_bank_published_nothing(self):
        with pytest.raises(AccountingBalanceUnavailableError) as excinfo:
            _accounting_balance({"balances": []}, "EUR")
        assert "aucun" in str(excinfo.value)

    def test_a_card_account_still_prefers_the_accounting_balance_when_there_is_one(self):
        payload = {
            "balances": [
                {"balance_amount": {"currency": "EUR", "amount": "0"}, "balance_type": "OTHR"},
                {"balance_amount": {"currency": "EUR", "amount": "406.70"}, "balance_type": "CLBD"},
            ]
        }
        assert _accounting_balance(payload, "EUR", is_card=True) == Decimal("406.70")

    def test_a_foreign_currency_balance_is_never_read_as_euros(self):
        payload = {
            "balances": [
                {"balance_amount": {"currency": "CHF", "amount": "406.70"}, "balance_type": "CLBD"}
            ]
        }
        with pytest.raises(AccountingBalanceUnavailableError):
            _accounting_balance(payload, "EUR", is_card=True)

    def test_that_same_balance_is_read_when_the_account_is_in_francs(self):
        """The refusal above is about a mismatch, not about foreign currencies:
        an account in Swiss francs reads its own balance."""
        payload = {
            "balances": [
                {"balance_amount": {"currency": "CHF", "amount": "406.70"}, "balance_type": "CLBD"}
            ]
        }
        assert _accounting_balance(payload, "CHF") == Decimal("406.70")

    def test_a_multi_currency_account_picks_its_own_currency_not_the_first_row(self):
        """One balance per currency under the same type. Reading the first would
        record francs as euros — the exact substitution §F forbids."""
        payload = {
            "balances": [
                {"balance_amount": {"currency": "CHF", "amount": "999.99"}, "balance_type": "CLBD"},
                {"balance_amount": {"currency": "EUR", "amount": "406.70"}, "balance_type": "CLBD"},
            ]
        }
        assert _accounting_balance(payload, "EUR") == Decimal("406.70")
        assert _accounting_balance(payload, "CHF") == Decimal("999.99")


class TestFetchWindow:
    def test_first_pass_uses_longest_and_an_ancient_date_from(
        self, session: Session, master_key: str, monkeypatch, sqlite_pg_insert
    ):
        account, link, client = _one_account_setup(
            session,
            master_key,
            accounting="1000.00",
            feed=[_raw("50", TODAY - timedelta(days=400), ref="old")],
            anchor_date=TODAY,
            anchor_balance=Decimal("1000"),
            last_synced_at=TODAY - timedelta(days=1),  # never synced marker
        )
        _install(monkeypatch, client)

        sync_user_accounts(session, USER, master_key)

        assert client.transaction_calls == [("uid-current", SEED_DATE_FROM, "longest")]
        assert SEED_DATE_FROM < date(2010, 1, 1)

    def test_later_passes_use_default_from_the_anchor(
        self, session: Session, master_key: str, monkeypatch, sqlite_pg_insert
    ):
        anchor = TODAY - timedelta(days=4)
        account, link, client = _one_account_setup(
            session,
            master_key,
            accounting="1000.00",
            feed=[],
            anchor_date=anchor,
            anchor_balance=Decimal("1000"),
        )
        _install(monkeypatch, client)

        sync_user_accounts(session, USER, master_key)

        assert client.transaction_calls == [("uid-current", anchor, "default")]

    def test_window_reaches_back_to_re_include_a_still_pending_row(
        self, session: Session, master_key: str, monkeypatch, sqlite_pg_insert
    ):
        pending_day = TODAY - timedelta(days=40)
        anchor = YESTERDAY
        account, link, client = _one_account_setup(
            session,
            master_key,
            accounting="1000.00",
            feed=[],
            anchor_date=anchor,
            anchor_balance=Decimal("1000"),
        )
        store_transactions(
            session,
            master_key,
            account.uuid,
            [_raw("42", pending_day, status="PDNG", ref="pend-1")],
        )
        _install(monkeypatch, client)

        sync_user_accounts(session, USER, master_key)

        # The anchor alone would have started the window at yesterday, leaving
        # the pending row unreachable by any correction.
        assert client.transaction_calls == [("uid-current", pending_day, "default")]

    def test_invalid_period_is_reframed_on_the_earliest_allowed_date(
        self, session: Session, master_key: str, monkeypatch, sqlite_pg_insert
    ):
        earliest = TODAY - timedelta(days=90)
        account, link, client = _one_account_setup(
            session,
            master_key,
            accounting="1000.00",
            feed=[_raw("10", TODAY - timedelta(days=30), ref="r1")],
            anchor_date=TODAY,
            anchor_balance=Decimal("1000"),
            last_synced_at=TODAY - timedelta(days=1),
        )
        client.period_error["uid-current"] = InvalidPeriodError(
            "WRONG_TRANSACTIONS_PERIOD",
            f"Transactions are available from {earliest.isoformat()}",
        )
        _install(monkeypatch, client)

        results = sync_user_accounts(session, USER, master_key)

        assert [r.status for r in results] == ["synced"]
        assert client.transaction_calls == [
            ("uid-current", SEED_DATE_FROM, "longest"),
            ("uid-current", earliest, "longest"),
        ]


# ---------------------------------------------------------------------------
# D3 — the reconciliation check
# ---------------------------------------------------------------------------


class TestReconciliation:
    def _period_feed(self):
        return [
            _raw("100", TODAY - timedelta(days=5), ref="r1"),
            _raw("30", TODAY - timedelta(days=2), direction="CRDT", ref="r2"),
        ]

    def test_a_check_that_holds_stores_no_gap(
        self, session: Session, master_key: str, monkeypatch, sqlite_pg_insert
    ):
        account, link, client = _one_account_setup(
            session,
            master_key,
            accounting="930.00",
            feed=self._period_feed(),
            anchor_date=TODAY - timedelta(days=10),
            anchor_balance=Decimal("1000"),
        )
        _install(monkeypatch, client)

        results = sync_user_accounts(session, USER, master_key)

        assert results[0].reconciliation_gap is None
        session.refresh(link)
        assert link.last_reconciliation_gap_enc is None

    def test_a_check_that_fails_stores_a_dated_gap(
        self, session: Session, master_key: str, monkeypatch, sqlite_pg_insert
    ):
        account, link, client = _one_account_setup(
            session,
            master_key,
            accounting="900.00",
            feed=self._period_feed(),
            anchor_date=TODAY - timedelta(days=10),
            anchor_balance=Decimal("1000"),
        )
        _install(monkeypatch, client)

        results = sync_user_accounts(session, USER, master_key)

        assert results[0].reconciliation_gap == Decimal("-30.00")
        session.refresh(link)
        assert Decimal(decrypt_data(link.last_reconciliation_gap_enc, master_key)) == Decimal("-30.00")
        # The gap is dated by the sync that found it.
        assert link.last_synced_at == TODAY

    def test_a_gap_is_cleared_once_the_check_holds_again(
        self, session: Session, master_key: str, monkeypatch, sqlite_pg_insert
    ):
        account, link, client = _one_account_setup(
            session,
            master_key,
            accounting="930.00",
            feed=self._period_feed(),
            anchor_date=TODAY - timedelta(days=10),
            anchor_balance=Decimal("1000"),
        )
        link.last_reconciliation_gap_enc = encrypt_data("-30.00", master_key)
        session.add(link)
        session.commit()
        _install(monkeypatch, client)

        sync_user_accounts(session, USER, master_key)

        session.refresh(link)
        assert link.last_reconciliation_gap_enc is None

    def test_pending_operations_are_excluded_from_the_computation(
        self, session: Session, master_key: str, monkeypatch, sqlite_pg_insert
    ):
        feed = self._period_feed() + [
            _raw("50", YESTERDAY, status="PDNG", ref="p1"),
        ]
        account, link, client = _one_account_setup(
            session,
            master_key,
            accounting="930.00",
            feed=feed,
            anchor_date=TODAY - timedelta(days=10),
            anchor_balance=Decimal("1000"),
        )
        _install(monkeypatch, client)

        results = sync_user_accounts(session, USER, master_key)

        # Counting the pending debit would have produced a 50 gap.
        assert results[0].reconciliation_gap is None

    def test_unconverted_foreign_currency_is_excluded_from_the_sums(
        self, session: Session, master_key: str, monkeypatch, sqlite_pg_insert
    ):
        feed = self._period_feed() + [
            _raw("12.63", TODAY - timedelta(days=3), currency="CHF", ref="chf-1"),
        ]
        account, link, client = _one_account_setup(
            session,
            master_key,
            accounting="930.00",
            feed=feed,
            anchor_date=TODAY - timedelta(days=10),
            anchor_balance=Decimal("1000"),
        )
        _install(monkeypatch, client)

        results = sync_user_accounts(session, USER, master_key)

        # Adding Swiss francs to euros would have made the check lie.
        assert results[0].reconciliation_gap is None

    def test_a_movement_booked_on_the_anchor_day_after_the_sync_is_not_a_gap(
        self, session: Session, master_key: str, monkeypatch, sqlite_pg_insert
    ):
        """The anchor is read mid-day, so the bank can book more rows that same
        day afterwards. They carry the anchor day's own date, and no date can
        tell them from the rows already counted — excluding the whole day from
        the period is what stops a permanent false gap."""
        anchor = YESTERDAY
        account, link, client = _one_account_setup(
            session,
            master_key,
            accounting="970.00",
            feed=[
                _raw("20", anchor, ref="counted-before-the-anchor-was-read"),
                _raw("30", anchor, ref="booked-after-the-anchor-was-read"),
            ],
            # Closing balance of the day *before* the anchor day.
            anchor_date=anchor,
            anchor_balance=Decimal("1020"),
        )
        _install(monkeypatch, client)

        results = sync_user_accounts(session, USER, master_key)

        assert results[0].reconciliation_gap is None
        assert results[0].reconciliation_status == "reconciled"

    def test_the_stored_anchor_excludes_the_movements_of_its_own_day(
        self, session: Session, master_key: str, monkeypatch, sqlite_pg_insert
    ):
        """The other half of the same rule: what is written is a day boundary,
        not the instant the balance was read."""
        account, link, client = _one_account_setup(
            session,
            master_key,
            accounting="900.00",
            feed=[_raw("100", TODAY, ref="booked-today")],
            anchor_date=TODAY - timedelta(days=3),
            anchor_balance=Decimal("1000"),
        )
        _install(monkeypatch, client)

        sync_user_accounts(session, USER, master_key)

        session.refresh(link)
        # 900 observed now, with 100 already debited today: the day opened at 1000.
        assert Decimal(decrypt_data(link.anchor_balance_enc, master_key)) == Decimal("1000.00")
        # The displayed balance stays the one the bank publishes.
        session.refresh(account)
        assert Decimal(decrypt_data(account.balance_enc, master_key)) == Decimal("900.00")

    def test_the_seeding_pass_reports_no_gap(
        self, session: Session, master_key: str, monkeypatch, sqlite_pg_insert
    ):
        # The bootstrap anchor is the *manually entered* balance, not a bank
        # reading: there is nothing comparable to reconcile against yet.
        account, link, client = _one_account_setup(
            session,
            master_key,
            accounting="930.00",
            feed=self._period_feed(),
            anchor_date=TODAY,
            anchor_balance=Decimal("1"),
            last_synced_at=TODAY - timedelta(days=1),
        )
        _install(monkeypatch, client)

        results = sync_user_accounts(session, USER, master_key)

        assert results[0].reconciliation_gap is None


# ---------------------------------------------------------------------------
# D4 — writing the curve
# ---------------------------------------------------------------------------


class TestCurve:
    def test_snapshots_are_written_from_the_anchor_to_yesterday_only(
        self, session: Session, master_key: str, monkeypatch, sqlite_pg_insert
    ):
        anchor = TODAY - timedelta(days=10)
        account, link, client = _one_account_setup(
            session,
            master_key,
            accounting="930.00",
            feed=[
                _raw("100", TODAY - timedelta(days=5), ref="r1"),
                _raw("30", TODAY - timedelta(days=2), direction="CRDT", ref="r2"),
            ],
            anchor_date=anchor,
            anchor_balance=Decimal("1000"),
        )
        _install(monkeypatch, client)

        sync_user_accounts(session, USER, master_key)

        rows = _history(session, account, master_key)
        assert [r.snapshot_date for r in rows] == [
            anchor + timedelta(days=i) for i in range(10)
        ]
        assert rows[-1].snapshot_date == YESTERDAY
        assert all(r.snapshot_date < TODAY for r in rows)

    def test_the_curve_walks_back_from_the_accounting_balance(
        self, session: Session, master_key: str, monkeypatch, sqlite_pg_insert
    ):
        anchor = TODAY - timedelta(days=10)
        account, link, client = _one_account_setup(
            session,
            master_key,
            accounting="930.00",
            feed=[
                _raw("100", TODAY - timedelta(days=5), ref="r1"),
                _raw("30", TODAY - timedelta(days=2), direction="CRDT", ref="r2"),
            ],
            anchor_date=anchor,
            anchor_balance=Decimal("1000"),
        )
        _install(monkeypatch, client)

        sync_user_accounts(session, USER, master_key)

        rows = _history(session, account, master_key)
        # The check holds, so the curve lands exactly on the previous anchor.
        assert _value_on(rows, anchor, master_key) == Decimal("1000.00")
        assert _value_on(rows, TODAY - timedelta(days=6), master_key) == Decimal("1000.00")
        assert _value_on(rows, TODAY - timedelta(days=5), master_key) == Decimal("900.00")
        assert _value_on(rows, TODAY - timedelta(days=3), master_key) == Decimal("900.00")
        assert _value_on(rows, TODAY - timedelta(days=2), master_key) == Decimal("930.00")
        assert _value_on(rows, YESTERDAY, master_key) == Decimal("930.00")

    def test_seeding_replaces_only_its_window_and_keeps_every_prior_snapshot(
        self, session: Session, master_key: str, monkeypatch, sqlite_pg_insert
    ):
        """BLOCKING (brief Step 2): the destructive trap.

        `import_bank_account_history(overwrite=True)` would delete this account's
        entire history. Seeding must replace the processed window and nothing else.
        """
        account, link, client = _one_account_setup(
            session,
            master_key,
            accounting="500.00",
            feed=[_raw("100", TODAY - timedelta(days=30), ref="seed-1")],
            anchor_date=TODAY,
            anchor_balance=Decimal("500"),
            last_synced_at=TODAY - timedelta(days=1),
        )
        account.created_at = datetime.now(timezone.utc) - timedelta(days=1500)
        session.add(account)
        session.commit()

        # Several years of manual entry, well before the seeding window.
        manual_days = [TODAY - timedelta(days=n) for n in (1400, 1000, 700, 400, 120)]
        manual = {day: _manual_snapshot(session, account, master_key, day, "5000.00") for day in manual_days}
        manual_uuids = {day: row.uuid for day, row in manual.items()}
        # ... plus one *inside* the window, which the bank data must overwrite.
        inside_day = TODAY - timedelta(days=20)
        inside_uuid = _manual_snapshot(session, account, master_key, inside_day, "1.00").uuid

        _install(monkeypatch, client)
        sync_user_accounts(session, USER, master_key)

        rows = _history(session, account, master_key)
        by_date = {r.snapshot_date: r for r in rows}

        # Nothing prior is lost, altered, or even re-inserted.
        for day in manual_days:
            assert day in by_date, f"manual snapshot {day} was destroyed"
            assert by_date[day].uuid == manual_uuids[day]
            assert Decimal(decrypt_data(by_date[day].total_value_enc, master_key)) == Decimal("5000.00")

        # And the window itself is genuinely replaced, not merely left alone.
        assert by_date[inside_day].uuid != inside_uuid
        assert Decimal(decrypt_data(by_date[inside_day].total_value_enc, master_key)) == Decimal("500.00")
        assert min(by_date) == TODAY - timedelta(days=1400)
        assert max(by_date) == YESTERDAY

    def test_a_later_pass_leaves_the_seeded_window_untouched(
        self, session: Session, master_key: str, monkeypatch, sqlite_pg_insert
    ):
        anchor = TODAY - timedelta(days=3)
        account, link, client = _one_account_setup(
            session,
            master_key,
            accounting="1000.00",
            feed=[],
            anchor_date=anchor,
            anchor_balance=Decimal("1000"),
        )
        older_uuid = _manual_snapshot(
            session, account, master_key, TODAY - timedelta(days=30), "777.00"
        ).uuid
        _install(monkeypatch, client)

        sync_user_accounts(session, USER, master_key)

        rows = _history(session, account, master_key)
        by_date = {r.snapshot_date: r for r in rows}
        assert by_date[TODAY - timedelta(days=30)].uuid == older_uuid
        assert _value_on(rows, YESTERDAY, master_key) == Decimal("1000.00")


# ---------------------------------------------------------------------------
# R12 — the sync order, and E — pending operations that vanish
# ---------------------------------------------------------------------------


class TestSyncOrder:
    """Ruling R12 forced current-before-card because cross-account deduplication
    kept a row on whichever account was stored first. That level is gone, so the
    order carries no meaning — but it still has to be *stable*, because the link
    query has no ORDER BY and Postgres guarantees nothing without one."""

    def test_the_order_is_deterministic(self, session: Session, master_key: str):
        from services.banking.sync import _in_stable_order

        links = [
            SimpleNamespace(uuid="c-uuid"),
            SimpleNamespace(uuid="a-uuid"),
            SimpleNamespace(uuid="b-uuid"),
        ]

        first = [link.uuid for link in _in_stable_order(links)]
        second = [link.uuid for link in _in_stable_order(list(reversed(links)))]

        assert first == second == ["a-uuid", "b-uuid", "c-uuid"]

    def test_every_linked_account_is_synced_exactly_once(
        self, session: Session, master_key: str, monkeypatch, sqlite_pg_insert
    ):
        _credentials(session, master_key)
        bank_session = _bank_session(
            session,
            master_key,
            [
                {"uid": "uid-card", "identification_hash": "ih-card", "cash_account_type": "CARD"},
                {"uid": "uid-current", "identification_hash": "ih-current", "cash_account_type": "CACC"},
            ],
        )
        card = _account(session, master_key, name="Carte")
        current = _account(session, master_key, name="Compte courant")
        _link(session, master_key, bank_session, card, "ih-card", "uid-card", TODAY - timedelta(days=2), Decimal("0"), TODAY - timedelta(days=2))
        _link(session, master_key, bank_session, current, "ih-current", "uid-current", TODAY - timedelta(days=2), Decimal("0"), TODAY - timedelta(days=2))

        client = FakeClient(
            balances={"uid-card": _balances("0.00"), "uid-current": _balances("0.00")},
            feeds={"uid-card": [], "uid-current": []},
        )
        _install(monkeypatch, client)

        sync_user_accounts(session, USER, master_key)

        assert sorted(client.balance_calls) == ["uid-card", "uid-current"]
        assert sorted(call[0] for call in client.transaction_calls) == ["uid-card", "uid-current"]


class TestNotReconcilableAccounts:
    """Ruling R19: a card account publishes a single OTHR balance and no CLBD.
    A curve is walked back *from* a balance and the reconciliation compares *to*
    one; with none that can be named, neither says anything. Measured, walking
    back from the OTHR of 0 of a debit-immédiat card invents +27 887 € eighteen
    months back — the spending history read as a balance."""

    def _card_setup(self, session, master_key, feed):
        return _one_account_setup(
            session,
            master_key,
            accounting="0.00",
            feed=feed,
            anchor_date=TODAY - timedelta(days=20),
            anchor_balance=Decimal("0"),
            cash_account_type="CARD",
        )

    def test_a_card_account_gets_an_anchor_but_never_a_curve(
        self, session: Session, master_key: str, monkeypatch, sqlite_pg_insert
    ):
        account, link, client = self._card_setup(
            session,
            master_key,
            feed=[
                _raw("100", TODAY - timedelta(days=10), ref="card-1"),
                _raw("40", TODAY - timedelta(days=4), ref="card-2"),
            ],
        )
        kept_day = TODAY - timedelta(days=15)
        kept_uuid = _manual_snapshot(session, account, master_key, kept_day, "123.00").uuid
        _install(monkeypatch, client)

        results = sync_user_accounts(session, USER, master_key)

        # Not an error and not a gap: a third outcome (ruling R18).
        assert results[0].status == "synced"
        assert results[0].reconciliation_status == "not_reconcilable"
        assert results[0].reconciliation_gap is None
        assert results[0].snapshots_written == 0

        # Every existing snapshot is untouched — the curve is not written at all.
        rows = _history(session, account, master_key)
        assert [r.uuid for r in rows] == [kept_uuid]
        assert Decimal(decrypt_data(rows[0].total_value_enc, master_key)) == Decimal("123.00")

        # The day's balance is still exact, because it is the anchor.
        session.refresh(link)
        session.refresh(account)
        assert link.anchor_date == TODAY
        assert link.last_synced_at == TODAY
        assert link.last_reconciliation_gap_enc is None
        assert Decimal(decrypt_data(account.balance_enc, master_key)) == Decimal("0.00")

    def test_a_card_account_still_stores_its_movements(
        self, session: Session, master_key: str, monkeypatch, sqlite_pg_insert
    ):
        """R19 withholds the curve, not the data: the rows are still stored."""
        account, link, client = self._card_setup(
            session, master_key, feed=[_raw("100", TODAY - timedelta(days=10), ref="card-1")]
        )
        _install(monkeypatch, client)

        results = sync_user_accounts(session, USER, master_key)

        assert results[0].inserted == 1
        rows = session.exec(
            select(BankTransaction).where(
                BankTransaction.account_id_bidx == hash_index(account.uuid, master_key)
            )
        ).all()
        assert len(rows) == 1


class TestVanishedPendingOperations:
    def test_a_pending_row_absent_from_the_feed_is_removed(
        self, session: Session, master_key: str, monkeypatch, sqlite_pg_insert
    ):
        pending_day = TODAY - timedelta(days=5)
        account, link, client = _one_account_setup(
            session,
            master_key,
            accounting="1000.00",
            feed=[_raw("20", TODAY - timedelta(days=4), ref="kept")],
            anchor_date=TODAY - timedelta(days=6),
            anchor_balance=Decimal("1020"),
        )
        store_transactions(
            session,
            master_key,
            account.uuid,
            [_raw("77", pending_day, status="PDNG", ref="ghost")],
        )
        _install(monkeypatch, client)

        sync_user_accounts(session, USER, master_key)

        rows = session.exec(
            select(BankTransaction).where(
                BankTransaction.account_id_bidx == hash_index(account.uuid, master_key)
            )
        ).all()
        statuses = {decrypt_data(r.status_enc, master_key) for r in rows}
        assert statuses == {"BOOK"}
        assert len(rows) == 1

    def test_a_pending_row_the_bank_refuses_to_report_is_kept(
        self, session: Session, master_key: str, monkeypatch, sqlite_pg_insert
    ):
        """Silence is not withdrawal: when the bank reframes the period, the
        days it never served are outside what its answer can settle."""
        pending_day = TODAY - timedelta(days=60)
        earliest = TODAY - timedelta(days=30)
        account, link, client = _one_account_setup(
            session,
            master_key,
            accounting="1000.00",
            feed=[],
            anchor_date=YESTERDAY,
            anchor_balance=Decimal("1000"),
        )
        store_transactions(
            session,
            master_key,
            account.uuid,
            [_raw("77", pending_day, status="PDNG", ref="far-pending")],
        )
        client.period_error["uid-current"] = InvalidPeriodError(
            "WRONG_TRANSACTIONS_PERIOD",
            f"Transactions are available from {earliest.isoformat()}",
        )
        _install(monkeypatch, client)

        sync_user_accounts(session, USER, master_key)

        assert client.transaction_calls == [
            ("uid-current", pending_day, "default"),
            ("uid-current", earliest, "default"),
        ]
        rows = session.exec(
            select(BankTransaction).where(
                BankTransaction.account_id_bidx == hash_index(account.uuid, master_key)
            )
        ).all()
        assert len(rows) == 1

    def test_a_booked_row_absent_from_the_feed_is_kept(
        self, session: Session, master_key: str, monkeypatch, sqlite_pg_insert
    ):
        booked_day = TODAY - timedelta(days=5)
        account, link, client = _one_account_setup(
            session,
            master_key,
            accounting="1000.00",
            feed=[],
            anchor_date=TODAY - timedelta(days=6),
            anchor_balance=Decimal("1000"),
        )
        store_transactions(session, master_key, account.uuid, [_raw("77", booked_day, ref="settled")]
        )
        _install(monkeypatch, client)

        sync_user_accounts(session, USER, master_key)

        rows = session.exec(
            select(BankTransaction).where(
                BankTransaction.account_id_bidx == hash_index(account.uuid, master_key)
            )
        ).all()
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# B5 — errors, branched on the business code
# ---------------------------------------------------------------------------


class TestErrors:
    def test_a_malformed_row_does_not_abort_the_sync(
        self, session: Session, master_key: str, monkeypatch, sqlite_pg_insert
    ):
        broken = _raw("10", TODAY - timedelta(days=2), ref="broken")
        del broken["credit_debit_indicator"]
        account, link, client = _one_account_setup(
            session,
            master_key,
            accounting="890.00",
            feed=[broken, _raw("100", TODAY - timedelta(days=3), ref="good")],
            anchor_date=TODAY - timedelta(days=5),
            anchor_balance=Decimal("1000"),
        )
        _install(monkeypatch, client)

        results = sync_user_accounts(session, USER, master_key)

        assert results[0].status == "synced"
        assert results[0].malformed == 1
        assert results[0].inserted == 1
        # The dropped row surfaces as a reconciliation gap rather than silence.
        assert results[0].reconciliation_gap == Decimal("-10.00")

    def test_an_expired_session_marks_the_link_to_reconnect_and_preserves_it(
        self, session: Session, master_key: str, monkeypatch, sqlite_pg_insert
    ):
        anchor = TODAY - timedelta(days=4)
        account, link, client = _one_account_setup(
            session,
            master_key,
            accounting="0",
            feed=[],
            anchor_date=anchor,
            anchor_balance=Decimal("1000"),
        )
        client.balances["uid-current"] = SessionInvalidError("EXPIRED_SESSION", "Session expired")
        _install(monkeypatch, client)

        results = sync_user_accounts(session, USER, master_key)

        assert results[0].status == "reconnect_required"
        session.refresh(link)
        assert link.anchor_date == anchor
        assert link.last_synced_at != TODAY
        bank_session = session.get(BankSession, link.session_uuid)
        assert bank_session.status == "EXPIRED"

    def test_one_failing_account_does_not_stop_the_others(
        self, session: Session, master_key: str, monkeypatch, sqlite_pg_insert
    ):
        _credentials(session, master_key)
        bank_session = _bank_session(
            session,
            master_key,
            [
                {"uid": "uid-current", "identification_hash": "ih-current", "cash_account_type": "CACC"},
                {"uid": "uid-card", "identification_hash": "ih-card", "cash_account_type": "CARD"},
            ],
        )
        current = _account(session, master_key, name="Compte courant")
        card = _account(session, master_key, name="Carte")
        _link(session, master_key, bank_session, current, "ih-current", "uid-current", TODAY - timedelta(days=2), Decimal("0"), TODAY - timedelta(days=2))
        _link(session, master_key, bank_session, card, "ih-card", "uid-card", TODAY - timedelta(days=2), Decimal("0"), TODAY - timedelta(days=2))

        from services.banking.errors import AspspUnavailableError

        client = FakeClient(
            balances={
                "uid-current": AspspUnavailableError("ASPSP_ERROR", "bank down"),
                "uid-card": _balances("0.00"),
            },
            feeds={"uid-card": []},
        )
        _install(monkeypatch, client)

        results = sync_user_accounts(session, USER, master_key)

        # Keyed by account, never by position: the sync order is now only
        # required to be stable, not to put any role first.
        by_account = {r.bank_account_uuid: r.status for r in results}
        assert by_account == {current.uuid: "error", card.uuid: "synced"}


# ---------------------------------------------------------------------------
# R6 — link metadata in the accounts payload
# ---------------------------------------------------------------------------


class TestAccountPayloadLinkMetadata:
    def test_a_linked_account_carries_its_sync_metadata(
        self, session: Session, master_key: str, monkeypatch, sqlite_pg_insert
    ):
        account, link, client = _one_account_setup(
            session,
            master_key,
            accounting="900.00",
            feed=[_raw("100", TODAY - timedelta(days=2), ref="r1")],
            anchor_date=TODAY - timedelta(days=5),
            anchor_balance=Decimal("1000"),
        )
        _install(monkeypatch, client)
        sync_user_accounts(session, USER, master_key)

        summary = get_user_bank_accounts(session, USER, master_key)
        payload = summary.accounts[0]

        assert payload.is_linked is True
        assert payload.last_synced_at == TODAY
        assert payload.link_status == "connecté"
        assert payload.reconciliation_gap is None
        assert payload.reconciliation_status == "reconciled"

    def test_a_never_synced_link_reports_no_last_sync(
        self, session: Session, master_key: str
    ):
        """The contract's `null = jamais`. The column is NOT NULL and the
        rattachement writes anchor_date - 1 day into it, so reporting it raw
        would show yesterday for an account the bank was never called for."""
        account, link, client = _one_account_setup(
            session,
            master_key,
            accounting="0.00",
            feed=[],
            anchor_date=TODAY,
            anchor_balance=Decimal("1000"),
            last_synced_at=TODAY - timedelta(days=1),
        )

        payload = get_user_bank_accounts(session, USER, master_key).accounts[0]

        assert payload.is_linked is True
        assert payload.last_synced_at is None
        assert payload.reconciliation_status is None

    def test_a_card_account_reports_the_third_outcome(
        self, session: Session, master_key: str, monkeypatch, sqlite_pg_insert
    ):
        account, link, client = _one_account_setup(
            session,
            master_key,
            accounting="0.00",
            feed=[],
            anchor_date=TODAY - timedelta(days=5),
            anchor_balance=Decimal("0"),
            cash_account_type="CARD",
        )
        _install(monkeypatch, client)
        sync_user_accounts(session, USER, master_key)

        payload = get_user_bank_accounts(session, USER, master_key).accounts[0]

        assert payload.reconciliation_status == "not_reconcilable"
        assert payload.reconciliation_gap is None
        # Distinct from the consent state, which is unaffected.
        assert payload.link_status == "connecté"

    def test_an_unlinked_account_carries_no_link_metadata(
        self, session: Session, master_key: str
    ):
        _account(session, master_key, user_uuid="lonely-user")

        summary = get_user_bank_accounts(session, "lonely-user", master_key)
        payload = summary.accounts[0]

        assert payload.is_linked is False
        assert payload.last_synced_at is None
        assert payload.link_status is None
        assert payload.reconciliation_gap is None

    def test_a_gap_and_a_dead_consent_are_surfaced(
        self, session: Session, master_key: str, monkeypatch, sqlite_pg_insert
    ):
        account, link, client = _one_account_setup(
            session,
            master_key,
            accounting="800.00",
            feed=[_raw("100", TODAY - timedelta(days=2), ref="r1")],
            anchor_date=TODAY - timedelta(days=5),
            anchor_balance=Decimal("1000"),
        )
        _install(monkeypatch, client)
        sync_user_accounts(session, USER, master_key)

        bank_session = session.get(BankSession, link.session_uuid)
        bank_session.status = "EXPIRED"
        session.add(bank_session)
        session.commit()

        payload = get_user_bank_accounts(session, USER, master_key).accounts[0]

        assert payload.reconciliation_gap == Decimal("-100.00")
        assert payload.reconciliation_status == "gap"
        assert payload.link_status == "à reconnecter"


# ---------------------------------------------------------------------------
# The real captured payloads (skipped when vendor-docs is absent)
# ---------------------------------------------------------------------------


SPIKE_DIR = Path(__file__).resolve().parents[3] / "vendor-docs" / "spike"


class TestCapturedPayloads:
    def test_the_real_current_account_feed_seeds_a_full_curve(
        self, session: Session, master_key: str, monkeypatch, sqlite_pg_insert
    ):
        capture = SPIKE_DIR / "tx_courant.json"
        if not capture.exists():
            pytest.skip("captured payloads live outside the repository")
        feed = json.loads(capture.read_text())

        account, link, client = _one_account_setup(
            session,
            master_key,
            accounting="406.70",
            real_time="244.07",
            feed=feed,
            anchor_date=TODAY,
            anchor_balance=Decimal("406.70"),
            last_synced_at=TODAY - timedelta(days=1),
        )
        _install(monkeypatch, client)

        results = sync_user_accounts(session, USER, master_key)

        assert results[0].status == "synced"
        # 297 rows, two of them without a booking_date, one in CHF, three pending.
        assert results[0].inserted > 250
        rows = _history(session, account, master_key)
        assert rows, "the seeding pass wrote no curve"
        assert max(r.snapshot_date for r in rows) == YESTERDAY


class TestForeignCurrencyAccount:
    """A whole account denominated in dollars, end to end (points 1 to 4).

    The rule the branch settles on: the account's own currency travels all the
    way to the reconciliation, and euros begin at the curve — which is a euro
    store, since `get_all_bank_accounts_history` adds the accounts up by date.
    """

    def test_the_dollar_balance_is_read_anchored_and_reconciled_without_conversion(
        self, session: Session, master_key: str, monkeypatch, sqlite_pg_insert
    ):
        account, link, client = _one_account_setup(
            session,
            master_key,
            accounting="900.00",
            feed=[_raw("100", TODAY - timedelta(days=1), currency="USD", ref="usd-1")],
            anchor_date=TODAY - timedelta(days=3),
            anchor_balance=Decimal("1000.00"),
            currency="USD",
        )
        _install(monkeypatch, client)
        # 1 USD = 0.90 EUR every day of the window.
        monkeypatch.setattr(
            "services.bank.get_historical_exchange_rates_db",
            lambda s, c, f, t: {d: Decimal("0.90") for d in _days(f, t)},
        )

        [result] = sync_user_accounts(session, USER, master_key)

        assert result.status == "synced"
        session.refresh(link)
        # The anchor stays in dollars: 900 read from the bank, not 810.
        assert Decimal(decrypt_data(link.anchor_balance_enc, master_key)) == Decimal("900.00")
        # 1000 - 100 = 900 in dollars, so no gap. Converting either side first
        # would have invented one out of the exchange rate alone (ruling R18).
        assert link.last_reconciliation_gap_enc is None

    def test_the_curve_is_stored_in_euros(
        self, session: Session, master_key: str, monkeypatch, sqlite_pg_insert
    ):
        account, link, client = _one_account_setup(
            session,
            master_key,
            accounting="900.00",
            feed=[_raw("100", TODAY - timedelta(days=1), currency="USD", ref="usd-1")],
            anchor_date=TODAY - timedelta(days=3),
            anchor_balance=Decimal("1000.00"),
            currency="USD",
        )
        _install(monkeypatch, client)
        monkeypatch.setattr(
            "services.bank.get_historical_exchange_rates_db",
            lambda s, c, f, t: {d: Decimal("0.90") for d in _days(f, t)},
        )

        sync_user_accounts(session, USER, master_key)

        rows = _history(session, account, master_key)
        # Yesterday closes at 900 USD → 810 EUR; the day before at 1000 → 900.
        assert _value_on(rows, TODAY - timedelta(days=1), master_key) == Decimal("810.00")
        assert _value_on(rows, TODAY - timedelta(days=2), master_key) == Decimal("900.00")

    def test_each_day_converts_at_its_own_rate(
        self, session: Session, master_key: str, monkeypatch, sqlite_pg_insert
    ):
        """A single rate across the window would draw the exchange rate's shape
        rather than the balance's."""
        account, link, client = _one_account_setup(
            session,
            master_key,
            accounting="1000.00",
            feed=[],
            anchor_date=TODAY - timedelta(days=3),
            anchor_balance=Decimal("1000.00"),
            currency="USD",
        )
        _install(monkeypatch, client)
        rates = {
            TODAY: Decimal("1.00"),
            TODAY - timedelta(days=1): Decimal("0.50"),
            TODAY - timedelta(days=2): Decimal("0.25"),
            TODAY - timedelta(days=3): Decimal("0.10"),
        }
        monkeypatch.setattr(
            "services.bank.get_historical_exchange_rates_db",
            lambda s, c, f, t: rates,
        )

        sync_user_accounts(session, USER, master_key)

        rows = _history(session, account, master_key)
        # The balance never moves — only the rate does.
        assert _value_on(rows, TODAY - timedelta(days=1), master_key) == Decimal("500.00")
        assert _value_on(rows, TODAY - timedelta(days=2), master_key) == Decimal("250.00")

    def test_a_euro_account_never_looks_a_rate_up(
        self, session: Session, master_key: str, monkeypatch, sqlite_pg_insert
    ):
        """The ordinary path must not depend on market data being reachable."""
        account, link, client = _one_account_setup(
            session,
            master_key,
            accounting="1000.00",
            feed=[],
            anchor_date=TODAY - timedelta(days=3),
            anchor_balance=Decimal("1000.00"),
        )
        _install(monkeypatch, client)

        def _fail(*args, **kwargs):
            raise AssertionError("a euro account must not need an exchange rate")

        monkeypatch.setattr("services.bank.get_historical_exchange_rates_db", _fail)

        assert sync_user_accounts(session, USER, master_key)[0].status == "synced"


def _days(start: date, end: date):
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)
