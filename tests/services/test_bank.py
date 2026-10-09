import uuid as uuid_mod
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlmodel import Session, select

from models.account_history import AccountHistory
from models.bank import BankAccount
from models.enums import AccountCategory, BankAccountType
from dtos.bank import BankAccountCreate, BankAccountUpdate, BankEntryRequest, BankHistoryEntry
from services.bank import (
    create_bank_account,
    delete_bank_account,
    delete_bank_account_history,
    UnconvertibleCurrencyError,
    get_bank_account,
    get_user_bank_accounts,
    replace_history_window,
    update_bank_account,
)
from services.bank_ledger import add_entry
from services.encryption import decrypt_data, encrypt_data, hash_index


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def sqlite_pg_insert(monkeypatch):
    """Replace pg_insert (PostgreSQL-specific) with its SQLite equivalent.

    SQLite has `ON CONFLICT DO NOTHING` too, and it has to be kept: a plain
    insert turns "this day is already written, leave it alone" into an
    IntegrityError, which is the whole point of the call.
    """
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    def _fake(table):
        class _Stmt:
            def values(self, rows):
                self._stmt = sqlite_insert(table).values(rows)
                return self

            def on_conflict_do_nothing(self, **kwargs):
                return self._stmt.on_conflict_do_nothing()

        return _Stmt()

    monkeypatch.setattr("services.bank.pg_insert", _fake)


def _opened(session: Session, user_uuid: str, master_key: str, amount: str, **fields):
    """An account with money on it: its first operation, as nobody types a balance."""
    created = create_bank_account(session, BankAccountCreate(**fields), user_uuid, master_key)
    add_entry(
        session, session.get(BankAccount, created.id),
        BankEntryRequest(day=date(2025, 1, 2), amount=Decimal(amount)), master_key,
    )
    return created


def _get_history_rows(session: Session, account_id_bidx: str) -> list[AccountHistory]:
    return session.exec(
        select(AccountHistory)
        .where(AccountHistory.account_id_bidx == account_id_bidx)
        .order_by(AccountHistory.snapshot_date)
    ).all()


def _value_on_date(rows: list[AccountHistory], d: date, master_key: str) -> Decimal:
    for row in rows:
        if row.snapshot_date == d:
            return Decimal(decrypt_data(row.total_value_enc, master_key))
    raise KeyError(f"No history row for {d}")


def test_create_bank_account(session: Session, master_key: str):
    user_uuid = "user_1"
    data = BankAccountCreate(
        name="Main Checking",
        account_type=BankAccountType.CHECKING,
        institution_name="Big Bank",
        identifier="FR76"
    )
    resp = create_bank_account(session, data, user_uuid, master_key)
    assert resp.name == "Main Checking"
    assert resp.balance == Decimal("0")
    assert resp.account_type == BankAccountType.CHECKING
    assert resp.institution_name == "Big Bank"
    assert resp.identifier == "FR76"
    db_acc = session.get(BankAccount, resp.id)
    assert db_acc is not None
    assert db_acc.user_uuid_bidx == hash_index(user_uuid, master_key)
    assert db_acc.balance_enc != "0"


def test_get_user_bank_accounts(session: Session, master_key: str):
    user_uuid = "user_1"
    _opened(session, user_uuid, master_key, "100", name="Acc 1", account_type=BankAccountType.CHECKING)
    _opened(session, user_uuid, master_key, "200", name="Acc 2", account_type=BankAccountType.SAVINGS)
    summary = get_user_bank_accounts(session, user_uuid, master_key)
    assert len(summary.accounts) == 2
    assert summary.total_balance == Decimal("300")


def test_get_bank_account(session: Session, master_key: str):
    user_uuid = "user_1"
    created = create_bank_account(session, BankAccountCreate(name="My Acc", account_type=BankAccountType.CHECKING), user_uuid, master_key)
    fetched = get_bank_account(session, created.id, user_uuid, master_key)
    assert fetched.name == "My Acc"
    assert get_bank_account(session, created.id, "user_2", master_key) is None
    assert get_bank_account(session, "non_existent", user_uuid, master_key) is None


def test_update_bank_account(session: Session, master_key: str):
    user_uuid = "user_1"
    created = _opened(session, user_uuid, master_key, "100", name="Old Name", account_type=BankAccountType.CHECKING)
    db_acc = session.get(BankAccount, created.id)
    updated = update_bank_account(session, db_acc, BankAccountUpdate(name="New Name", institution_name="New Inst", identifier="New ID"), master_key)
    assert updated.name == "New Name"
    assert updated.balance == Decimal("100")
    assert updated.institution_name == "New Inst"
    assert updated.identifier == "New ID"


def test_delete_bank_account(session: Session, master_key: str):
    user_uuid = "user_1"
    created = create_bank_account(session, BankAccountCreate(name="Del", account_type=BankAccountType.CHECKING), user_uuid, master_key)
    account_id_bidx = hash_index(created.id, master_key)
    user_bidx = hash_index(user_uuid, master_key)
    session.add(AccountHistory(
        uuid=str(uuid_mod.uuid4()),
        user_uuid_bidx=user_bidx,
        account_id_bidx=account_id_bidx,
        account_type=AccountCategory.BANK,
        snapshot_date=date(2025, 1, 1),
        total_value_enc=encrypt_data("0", master_key),
        total_invested_enc=encrypt_data("0", master_key),
    ))
    session.commit()

    assert delete_bank_account(session, created.id, master_key) is True
    assert session.get(BankAccount, created.id) is None
    assert _get_history_rows(session, account_id_bidx) == []
    assert delete_bank_account(session, "non_existent", master_key) is False


# ---------------------------------------------------------------------------
# History tests
# ---------------------------------------------------------------------------


def test_delete_bank_account_history(session: Session, master_key: str):
    """Deleting history removes all rows and returns the deleted count."""
    user_uuid = "user_del_hist"
    acc = create_bank_account(
        session,
        BankAccountCreate(name="Del Hist", account_type=BankAccountType.CHECKING),
        user_uuid,
        master_key,
    )
    account_id_bidx = hash_index(acc.id, master_key)
    user_bidx = hash_index(user_uuid, master_key)

    for d in [date(2025, 1, 1), date(2025, 1, 2), date(2025, 1, 3)]:
        session.add(AccountHistory(
            uuid=str(uuid_mod.uuid4()),
            user_uuid_bidx=user_bidx,
            account_id_bidx=account_id_bidx,
            account_type=AccountCategory.BANK,
            snapshot_date=d,
            total_value_enc=encrypt_data("1000", master_key),
            total_invested_enc=encrypt_data("1000", master_key),
        ))
    session.commit()

    assert len(_get_history_rows(session, account_id_bidx)) == 3
    deleted = delete_bank_account_history(session, acc.id, master_key)
    assert deleted == 3
    assert len(_get_history_rows(session, account_id_bidx)) == 0


# ---------------------------------------------------------------------------
# replace_history_window — the date-bounded replacement
# ---------------------------------------------------------------------------


def _seed_snapshot(session: Session, account, master_key: str, day: date, value: str) -> str:
    row = AccountHistory(
        uuid=str(uuid_mod.uuid4()),
        user_uuid_bidx=account.user_uuid_bidx,
        account_id_bidx=hash_index(account.uuid, master_key),
        account_type=AccountCategory.BANK,
        snapshot_date=day,
        total_value_enc=encrypt_data(value, master_key),
        total_invested_enc=encrypt_data(value, master_key),
    )
    session.add(row)
    session.commit()
    return row.uuid


def test_replace_history_window_touches_nothing_outside_the_window(
    session: Session, master_key: str, sqlite_pg_insert
):
    """The rows before and after the window are left as they are."""
    acc = create_bank_account(
        session,
        BankAccountCreate(name="Windowed", account_type=BankAccountType.CHECKING),
        "user_window",
        master_key,
    )
    db_acc = session.get(BankAccount, acc.id)
    account_id_bidx = hash_index(acc.id, master_key)

    before_uuid = _seed_snapshot(session, db_acc, master_key, date(2020, 1, 1), "111")
    inside_uuid = _seed_snapshot(session, db_acc, master_key, date(2025, 3, 2), "222")
    after_uuid = _seed_snapshot(session, db_acc, master_key, date(2025, 4, 1), "333")

    written = replace_history_window(
        session,
        db_acc,
        [
            BankHistoryEntry(snapshot_date=date(2025, 3, 1), value=Decimal("10")),
            BankHistoryEntry(snapshot_date=date(2025, 3, 2), value=Decimal("20")),
        ],
        master_key,
        date(2025, 3, 1),
        date(2025, 3, 3),
    )

    assert written == 2
    rows = {r.snapshot_date: r for r in _get_history_rows(session, account_id_bidx)}
    assert rows[date(2020, 1, 1)].uuid == before_uuid
    assert rows[date(2025, 4, 1)].uuid == after_uuid
    assert Decimal(decrypt_data(rows[date(2020, 1, 1)].total_value_enc, master_key)) == Decimal("111")
    assert Decimal(decrypt_data(rows[date(2025, 4, 1)].total_value_enc, master_key)) == Decimal("333")
    # Inside the window the old row is gone, replaced by the supplied value.
    assert rows[date(2025, 3, 2)].uuid != inside_uuid
    assert Decimal(decrypt_data(rows[date(2025, 3, 2)].total_value_enc, master_key)) == Decimal("20")


def test_replace_history_window_never_writes_today_or_later(
    session: Session, master_key: str, sqlite_pg_insert
):
    acc = create_bank_account(
        session,
        BankAccountCreate(name="Yesterday", account_type=BankAccountType.CHECKING),
        "user_yesterday",
        master_key,
    )
    db_acc = session.get(BankAccount, acc.id)
    today = date.today()

    written = replace_history_window(
        session,
        db_acc,
        [
            BankHistoryEntry(snapshot_date=today - timedelta(days=1), value=Decimal("10")),
            BankHistoryEntry(snapshot_date=today, value=Decimal("20")),
            BankHistoryEntry(snapshot_date=today + timedelta(days=1), value=Decimal("30")),
        ],
        master_key,
        today - timedelta(days=1),
        today + timedelta(days=1),
    )

    rows = _get_history_rows(session, hash_index(acc.id, master_key))
    assert written == 1
    assert [r.snapshot_date for r in rows] == [today - timedelta(days=1)]


def test_replace_history_window_clears_a_window_it_has_no_entries_for(
    session: Session, master_key: str, sqlite_pg_insert
):
    """A window with nothing left in it still empties: that is how a snapshot
    built on a movement the bank later withdrew disappears."""
    acc = create_bank_account(
        session,
        BankAccountCreate(name="Cleared", account_type=BankAccountType.CHECKING),
        "user_cleared",
        master_key,
    )
    db_acc = session.get(BankAccount, acc.id)
    kept_uuid = _seed_snapshot(session, db_acc, master_key, date(2025, 1, 1), "111")
    _seed_snapshot(session, db_acc, master_key, date(2025, 2, 5), "222")

    written = replace_history_window(
        session, db_acc, [], master_key, date(2025, 2, 1), date(2025, 2, 28)
    )

    rows = _get_history_rows(session, hash_index(acc.id, master_key))
    assert written == 0
    assert [r.uuid for r in rows] == [kept_uuid]


def test_the_total_adds_up_in_euros_not_across_currencies(session: Session, master_key: str):
    """Two accounts, one in euros and one in dollars. Adding the raw figures
    would total dollars with euros; the answer must be the euro value."""
    from unittest.mock import patch

    user_uuid = "user_1"

    _opened(session, user_uuid, master_key, "100", name="Courant", account_type=BankAccountType.CHECKING)
    with patch("services.bank.has_exchange_rate", return_value=True):
        _opened(
            session, user_uuid, master_key, "200",
            name="Dollars", account_type=BankAccountType.CHECKING, currency="USD",
        )

    with patch("services.bank.has_exchange_rate", return_value=True):
        with patch("services.bank.get_exchange_rate", side_effect=lambda s, f, t: (
            Decimal("1") if f == "EUR" else Decimal("0.90")
        )):
            summary = get_user_bank_accounts(session, user_uuid, master_key)

    # 100 EUR + 200 USD × 0.90 = 280, never 300.
    assert summary.total_balance == Decimal("280.00")
    assert {a.name: a.currency for a in summary.accounts} == {"Courant": "EUR", "Dollars": "USD"}


def test_a_currency_with_no_published_rate_is_refused(session: Session, master_key: str):
    """A currency that cannot be converted would be added to the euro total
    one-for-one, silently. Refusing at the door is the whole point."""
    from unittest.mock import patch

    with patch("services.bank.has_exchange_rate", return_value=False):
        with pytest.raises(UnconvertibleCurrencyError):
            create_bank_account(
                session,
                BankAccountCreate(
                    name="Exotique",
                    account_type=BankAccountType.CHECKING,
                    currency="XAF",
                ),
                "user_1",
                master_key,
            )


def test_euros_never_need_a_rate_lookup(session: Session, master_key: str):
    """The default path must not depend on market data being reachable."""
    from unittest.mock import patch

    def _fail(*args, **kwargs):
        raise AssertionError("EUR must not be looked up")

    with patch("services.market._get_market_info_internal", _fail):
        account = create_bank_account(
            session,
            BankAccountCreate(name="Courant", account_type=BankAccountType.CHECKING),
            "user_1",
            master_key,
        )
    assert account.currency == "EUR"


def test_the_total_is_withheld_when_a_currency_lost_its_rate(session: Session, master_key: str):
    """The guard at creation cannot cover this: a rate can stop being published
    after the account exists. Adding it one-for-one would put a wrong total on
    screen with nothing saying so, so no total is given at all."""
    from unittest.mock import patch

    with patch("services.bank.has_exchange_rate", return_value=True):
        _opened(
            session, "user_1", master_key, "500",
            name="Exotique", account_type=BankAccountType.CHECKING, currency="XAF",
        )

    with patch("services.bank.has_exchange_rate", return_value=False):
        summary = get_user_bank_accounts(session, "user_1", master_key)

    assert summary.total_balance is None
    # The accounts themselves stay readable — only the total is withheld.
    assert [a.name for a in summary.accounts] == ["Exotique"]


def test_an_imported_curve_is_stored_in_euros(
    session: Session, master_key: str, sqlite_pg_insert, monkeypatch
):
    """`account_history` is a euro store. A curve in francs must not land
    there at face value."""
    from unittest.mock import patch

    user_uuid = "user_import_currency"
    with patch("services.bank.has_exchange_rate", return_value=True):
        acc = create_bank_account(
            session,
            BankAccountCreate(
                name="Suisse",
                account_type=BankAccountType.CHECKING,
                currency="CHF",
            ),
            user_uuid,
            master_key,
        )
    db_acc = session.get(BankAccount, acc.id)
    account_id_bidx = hash_index(acc.id, master_key)

    # Each day at its own rate — a single rate would draw the exchange rate's
    # shape instead of the balance's.
    rates = {date(2025, 1, 1): Decimal("0.90"), date(2025, 1, 2): Decimal("0.95")}
    monkeypatch.setattr(
        "services.bank.get_historical_exchange_rates_db",
        lambda session, currency, start, end: {
            day: rates.get(day, Decimal("1.00"))
            for day in _days(start, end)
        },
    )

    replace_history_window(
        session,
        db_acc,
        [BankHistoryEntry(snapshot_date=day, value=Decimal("1000")) for day in rates],
        master_key, date(2025, 1, 1), date(2025, 1, 2),
    )

    rows = _get_history_rows(session, account_id_bidx)
    assert _value_on_date(rows, date(2025, 1, 1), master_key) == Decimal("900.00")
    # The balance stands still; its euro value follows the rate.
    assert _value_on_date(rows, date(2025, 1, 2), master_key) == Decimal("950.00")


def test_a_euro_import_never_looks_a_rate_up(
    session: Session, master_key: str, sqlite_pg_insert, monkeypatch
):
    """The conversion is skipped outright for euro accounts, which is all of
    them until one is opened elsewhere."""
    def _fail(*args, **kwargs):
        raise AssertionError("a euro account must not need an exchange rate")

    monkeypatch.setattr("services.bank.get_historical_exchange_rates_db", _fail)

    acc = create_bank_account(
        session,
        BankAccountCreate(name="Courant", account_type=BankAccountType.CHECKING),
        "user_import_eur",
        master_key,
    )
    written = replace_history_window(
        session,
        session.get(BankAccount, acc.id),
        [BankHistoryEntry(snapshot_date=date(2025, 1, 1), value=Decimal("1000"))],
        master_key, date(2025, 1, 1), date(2025, 1, 1),
    )
    assert written > 0


def _days(start: date, end: date):
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)
