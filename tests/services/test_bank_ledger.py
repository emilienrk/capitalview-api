"""
An unsynced account's balance and curve, derived from its operations
(services/bank_ledger.py, docs/bank-ledger.md).

Every account here starts from a daily history, as real ones do: the store has
carried a curve for each account since long before the operations did.
"""
import textwrap
from datetime import date, timedelta
from decimal import Decimal

from sqlmodel import Session, select

from dtos.bank import BankAccountCreate, BankEntryKind, BankEntryRequest, BankHistoryEntry
from dtos.banking import CashflowType, TypeSource
from dtos.imports import ImportConfirmRequest
from models.bank import BankAccount
from models.banking import BankTransaction
from models.enums import BankAccountType
from services.bank import create_bank_account, get_bank_account_history, import_bank_account_history
from services.bank_ledger import add_entry, delete_operation, ensure_ledgers
from services.banking.flows import compute_real_flows, list_month_transactions
from services.banking.transactions import ORIGIN_FORECAST, row_origin, store_entry
from services.encryption import decrypt_data, encrypt_data, hash_index
from services.imports.bank_csv import parse_bank_points, parse_bank_transactions
from services.imports.registry import get_parser

USER = "ledger_user"
YESTERDAY = date.today() - timedelta(days=1)

STATEMENT = textwrap.dedent("""\
    date,amount,label
    2024-01-15,-42.50,CARTE FNAC
    2024-01-31,1200.00,VIREMENT SALAIRE
    2024-02-03,-850.00,VIREMENT LIVRET A
""")


def _account(session: Session, master_key: str, name: str = "Livret A",
             kind: BankAccountType = BankAccountType.CHECKING) -> BankAccount:
    created = create_bank_account(
        session, BankAccountCreate(name=name, balance=Decimal("0"), account_type=kind), USER, master_key
    )
    return session.get(BankAccount, created.id)


def _legacy(session: Session, master_key: str, balance: Decimal, points: list[tuple[date, Decimal]]) -> BankAccount:
    """An account as it stood before the ledger: a typed balance, and a daily
    curve repeating it."""
    account = _account(session, master_key)
    account.ledger_version = None
    account.balance_enc = encrypt_data(str(balance), master_key)
    session.add(account)
    session.commit()
    import_bank_account_history(
        session, account, [BankHistoryEntry(snapshot_date=d, value=v) for d, v in points], master_key
    )
    return account


def _curve(session: Session, master_key: str, account: BankAccount) -> dict[date, Decimal]:
    return {s.snapshot_date: s.total_value for s in get_bank_account_history(session, account.uuid, master_key)}


def _balance(session: Session, master_key: str, account: BankAccount) -> Decimal:
    session.refresh(account)
    return Decimal(decrypt_data(account.balance_enc, master_key))


def _rows(session: Session, master_key: str, account: BankAccount) -> list[BankTransaction]:
    return list(session.exec(
        select(BankTransaction).where(BankTransaction.account_id_bidx == hash_index(account.uuid, master_key))
    ).all())


def _origins(session: Session, master_key: str, account: BankAccount) -> list[str | None]:
    return sorted((row_origin(r, master_key) or "") for r in _rows(session, master_key, account))


def _import(session, master_key, account, csv=STATEMENT, options=None, rows=None):
    parser = get_parser("generic_bank_transactions")
    rows = rows if rows is not None else parse_bank_transactions(csv, {})[0]
    return parser.execute(
        session, account.uuid,
        ImportConfirmRequest(account_id=account.uuid, bank_transactions=rows, options=options or {}),
        master_key,
    )


def _preview(session, master_key, account, csv=STATEMENT, options=None):
    return get_parser("generic_bank_transactions").preview(
        session, csv, options or {}, account_id=account.uuid, master_key=master_key
    )


# ─── The 7 October case ──────────────────────────────────────────────────


def test_a_reimport_moves_a_balance_the_history_had_frozen(session, master_key):
    """The balance was a typed figure the history repeated every day; the
    operations now give it, and a second import changes nothing."""
    account = _legacy(session, master_key, Decimal("3000"), [(date(2024, 1, 1), Decimal("3000"))])

    _import(session, master_key, account)
    again = _import(session, master_key, account)

    assert again.imported_count == 0
    assert _balance(session, master_key, account) == Decimal("3307.50")
    curve = _curve(session, master_key, account)
    assert curve[date(2024, 1, 10)] == Decimal("3000.00")
    assert curve[date(2024, 2, 3)] == Decimal("3307.50")
    assert max(curve) == YESTERDAY
    assert curve[YESTERDAY] == Decimal("3307.50")


def test_the_curve_is_carried_to_yesterday_from_the_operations(session, master_key, monkeypatch):
    from services.bank_ledger import refresh_ledgers
    from models.account_history import AccountHistory
    import sqlalchemy as sa

    account = _account(session, master_key)
    _import(session, master_key, account, options={"initial_balance": "100"})
    session.exec(sa.delete(AccountHistory).where(
        AccountHistory.account_id_bidx == hash_index(account.uuid, master_key),
        AccountHistory.snapshot_date >= YESTERDAY - timedelta(days=3),
    ))
    session.commit()

    refresh_ledgers(session, account.user_uuid_bidx, master_key)

    assert _curve(session, master_key, account)[YESTERDAY] == Decimal("407.50")


# ─── Entries by hand ─────────────────────────────────────────────────────


def test_a_typed_operation_is_replaced_by_the_statement_holding_it(session, master_key):
    account = _account(session, master_key)
    add_entry(session, account, BankEntryRequest(
        kind=BankEntryKind.OPERATION, day=date(2024, 1, 15), amount=Decimal("-42.50"), label="fnac"
    ), master_key)

    preview = _preview(session, master_key, account)
    assert [r.status for r in preview.bank_transactions] == ["replaces_manual", "new", "new"]
    assert preview.bank_balance_after == Decimal("307.50")

    _import(session, master_key, account)

    assert _origins(session, master_key, account) == ["", "", ""]
    assert _balance(session, master_key, account) == Decimal("307.50")


def test_twins_facing_one_typed_operation_are_ambiguous(session, master_key):
    account = _account(session, master_key)
    add_entry(session, account, BankEntryRequest(
        kind=BankEntryKind.OPERATION, day=date(2024, 1, 15), amount=Decimal("-42.50")
    ), master_key)

    preview = _preview(
        session, master_key, account,
        "date,amount,label\n2024-01-15,-42.50,CARTE FNAC\n2024-01-15,-42.50,CARTE FNAC\n",
    )

    assert [r.status for r in preview.bank_transactions] == ["ambiguous", "ambiguous"]


def test_an_excluded_row_is_left_out_and_keeps_its_twin_s_reference(session, master_key):
    account = _account(session, master_key)
    csv = "date,amount,label\n2024-01-15,-42.50,CARTE FNAC\n2024-01-15,-42.50,CARTE FNAC\n"
    rows, _ = parse_bank_transactions(csv, {})
    rows[0].excluded = True
    _import(session, master_key, account, rows=rows)

    assert len(_rows(session, master_key, account)) == 1
    # Imported whole later: the twin already stored is recognised, not doubled.
    assert _import(session, master_key, account, csv).imported_count == 1
    assert _balance(session, master_key, account) == Decimal("-85.00")


def test_a_stated_balance_becomes_an_adjustment_that_counts_nowhere(session, master_key):
    checking = _account(session, master_key, "Courant")
    savings = _account(session, master_key, "Livret", BankAccountType.LIVRET_A)
    _import(session, master_key, checking, "date,amount,label\n2024-01-10,-500.00,VIR LIVRET\n")

    dry = add_entry(session, savings, BankEntryRequest(
        kind=BankEntryKind.BALANCE, day=date(2024, 1, 10), balance=Decimal("500")
    ), master_key, dry_run=True)
    assert (dry.adjustment, dry.balance_now_before, dry.balance_now_after) == (Decimal("500"), 0, Decimal("500"))
    assert _rows(session, master_key, savings) == []

    add_entry(session, savings, BankEntryRequest(
        kind=BankEntryKind.BALANCE, day=date(2024, 1, 10), balance=Decimal("500")
    ), master_key)

    assert _balance(session, master_key, savings) == Decimal("500")
    items = {i.account_name: i for i in list_month_transactions(session, USER, master_key, "2024-01").transactions}
    assert items["Livret"].origin == "adjustment"
    assert items["Livret"].type_source is TypeSource.ADJUSTMENT
    assert items["Livret"].cashflow_type is CashflowType.NEUTRAL
    # Never paired with the debit that reads like a transfer to it.
    assert items["Courant"].transfer_status is None and items["Livret"].transfer_status is None
    flows = compute_real_flows(session, USER, master_key, months=1, today=date(2024, 1, 31), account_id=savings.uuid)
    assert flows.inflow == 0


def test_a_stated_balance_replaces_the_forecasts_up_to_its_day(session, master_key):
    account = _account(session, master_key)
    store_entry(session, master_key, account.uuid, date(2024, 1, 5), Decimal("1000"), "Salaire", "EUR", ORIGIN_FORECAST)
    store_entry(session, master_key, account.uuid, date(2024, 2, 5), Decimal("1000"), "Salaire", "EUR", ORIGIN_FORECAST)
    session.commit()

    response = add_entry(session, account, BankEntryRequest(
        kind=BankEntryKind.BALANCE, day=date(2024, 1, 20), balance=Decimal("900")
    ), master_key)

    assert response.forecasts_replaced == 1
    assert response.adjustment == Decimal("900")
    assert _balance(session, master_key, account) == Decimal("1900")


def test_a_deleted_operation_leaves_the_balance(session, master_key):
    account = _account(session, master_key)
    _import(session, master_key, account)
    row = next(r for r in _rows(session, master_key, account) if decrypt_data(r.amount_enc, master_key) == "850")

    delete_operation(session, account, row, master_key)

    assert _balance(session, master_key, account) == Decimal("1157.50")


# ─── Balance files ───────────────────────────────────────────────────────


def _import_balances(session, master_key, account, csv):
    parser = get_parser("generic_bank")
    points, _ = parse_bank_points(csv, parser.effective_options({}))
    return parser.execute(
        session, account.uuid, ImportConfirmRequest(account_id=account.uuid, bank_points=points), master_key
    )


def test_a_balance_file_imported_twice_adjusts_once(session, master_key):
    account = _account(session, master_key)
    csv = "snapshot_date,value\n2024-01-31,1000\n2024-03-31,1500\n"

    assert _import_balances(session, master_key, account, csv).imported_count == 2
    assert _import_balances(session, master_key, account, csv).imported_count == 0

    curve = _curve(session, master_key, account)
    assert curve[date(2024, 2, 15)] == Decimal("1000.00")
    assert curve[date(2024, 3, 31)] == Decimal("1500.00")
    assert _balance(session, master_key, account) == Decimal("1500")


# ─── Reality replaces the rest ───────────────────────────────────────────


def test_an_import_replaces_the_adjustments_and_forecasts_of_its_period(session, master_key):
    account = _account(session, master_key)
    add_entry(session, account, BankEntryRequest(
        kind=BankEntryKind.BALANCE, day=date(2024, 1, 20), balance=Decimal("700")
    ), master_key)
    # Outside the file's period: kept.
    add_entry(session, account, BankEntryRequest(
        kind=BankEntryKind.BALANCE, day=date(2024, 3, 1), balance=Decimal("1000")
    ), master_key)
    store_entry(session, master_key, account.uuid, date(2024, 2, 1), Decimal("50"), "Loyer", "EUR", ORIGIN_FORECAST)
    session.commit()

    preview = _preview(session, master_key, account)
    assert sorted((e.day, e.origin) for e in preview.bank_replaced) == [
        (date(2024, 1, 20), "adjustment"), (date(2024, 2, 1), "forecast"),
    ]

    result = _import(session, master_key, account)

    assert result.replaced_count == 2
    assert _origins(session, master_key, account) == ["", "", "", "adjustment"]
    # The March adjustment keeps its amount (the known limit): 1000 - 700.
    assert _balance(session, master_key, account) == Decimal("607.50")


def test_an_opening_balance_given_at_import_is_an_adjustment_on_its_eve(session, master_key):
    account = _account(session, master_key)
    _import(session, master_key, account, options={"initial_balance": "2000"})

    assert _preview(session, master_key, account).bank_curve.opening_balance == Decimal("2000")
    assert _import(session, master_key, account, options={"initial_balance": "2000"}).imported_count == 0
    assert _origins(session, master_key, account) == ["", "", "", "adjustment"]
    assert _balance(session, master_key, account) == Decimal("2307.50")


# ─── Converting what predates the ledger ─────────────────────────────────


def test_a_balance_only_account_keeps_its_curve_through_the_conversion(session, master_key):
    account = _legacy(session, master_key, Decimal("1500"), [
        (date(2024, 1, 1), Decimal("1000")), (date(2024, 3, 1), Decimal("1500")),
    ])
    before = _curve(session, master_key, account)

    assert ensure_ledgers(session, account.user_uuid_bidx, master_key) == 1
    assert _curve(session, master_key, account) == before
    assert len(_rows(session, master_key, account)) == 2

    assert ensure_ledgers(session, account.user_uuid_bidx, master_key) == 0
    assert len(_rows(session, master_key, account)) == 2
    assert _balance(session, master_key, account) == Decimal("1500")


def test_a_balance_typed_since_the_last_snapshot_survives_the_conversion(session, master_key):
    account = _legacy(session, master_key, Decimal("1200"), [(date(2024, 1, 1), Decimal("1000"))])

    ensure_ledgers(session, account.user_uuid_bidx, master_key)

    assert _balance(session, master_key, account) == Decimal("1200")


def test_operations_win_over_the_history_after_them(session, master_key):
    """Only the eve of the first operation is carried; the frozen days after
    it said nothing the operations do not."""
    account = _legacy(session, master_key, Decimal("3000"), [(date(2024, 1, 1), Decimal("3000"))])
    rows, _ = parse_bank_transactions(STATEMENT, {})
    from services.banking.transactions import store_transactions
    from services.imports.bank_csv import _raws, _with_references
    store_transactions(session, master_key, account.uuid, _raws(_with_references(rows), "EUR"))

    ensure_ledgers(session, account.user_uuid_bidx, master_key)

    assert _balance(session, master_key, account) == Decimal("3307.50")
    assert _origins(session, master_key, account) == ["", "", "", "adjustment"]
