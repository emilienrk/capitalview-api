import textwrap
from datetime import date
from decimal import Decimal

from sqlmodel import select

from dtos.bank import BankAccountCreate
from dtos.imports import ImportConfirmRequest
from models.account_history import AccountHistory
from models.banking import BankTransaction
from models.enums import BankAccountType
from services.bank import create_bank_account
from services.encryption import decrypt_data, hash_index

from services.imports.bank_csv import (
    _with_references,
    parse_bank_points,
    parse_bank_transactions,
)
from services.imports.registry import get_parser

BALANCE_CSV = textwrap.dedent("""\
    Date;Solde
    15/01/2024;1000,00
    15/01/2024;1050,00
    20/01/2024;980,50
""")

DELTA_CSV = textwrap.dedent("""\
    Date;Montant
    10/01/2024;500,00
    12/01/2024;-100,00
    12/01/2024;20,00
""")


def test_bank_balance_mode_last_wins():
    points, warnings = parse_bank_points(BALANCE_CSV, {
        "mapping": {"date": "Date", "balance": "Solde"},
        "date_format": "%d/%m/%Y",
        "decimal_separator": ",",
    })
    assert len(points) == 2
    assert points[0].snapshot_date == date(2024, 1, 15)
    assert points[0].value == Decimal("1050.00")  # last row for the date wins
    assert points[1].value == Decimal("980.50")


def test_bank_delta_mode_accumulates():
    points, _ = parse_bank_points(DELTA_CSV, {
        "mapping": {"date": "Date", "amount": "Montant"},
        "bank_mode": "delta",
        "initial_balance": "100",
        "date_format": "%d/%m/%Y",
        "decimal_separator": ",",
    })
    assert len(points) == 2
    assert points[0].value == Decimal("600.00")   # 100 + 500
    assert points[1].value == Decimal("520.00")   # 600 - 100 + 20


def test_bank_unreadable_rows_warn():
    csv_content = "Date;Solde\ngarbage;xx\n15/01/2024;100,00\n"
    points, warnings = parse_bank_points(csv_content, {
        "mapping": {"date": "Date", "balance": "Solde"},
        "date_format": "%d/%m/%Y",
        "decimal_separator": ",",
    })
    assert len(points) == 1
    assert warnings and "illisible" in warnings[0]


NATIVE_CSV = textwrap.dedent("""\
    snapshot_date,value
    2024-01-31,12500.00
    2024-02-29,13200.50
""")

NATIVE_FR_CSV = textwrap.dedent("""\
    snapshot_date;value
    31/01/2024;12 500,00
    29/02/2024;13200,50
""")


def test_native_parser_is_registered():
    assert get_parser("native_bank") is not None


def test_native_parser_detects_its_own_header():
    parser = get_parser("native_bank")
    assert parser.detect(NATIVE_CSV) == 1.0
    assert parser.detect("Date;Solde\n15/01/2024;1000,00\n") == 0.0


def test_native_parser_offers_a_template():
    parser = get_parser("native_bank")
    assert parser.template_csv is not None
    assert parser.template_csv.splitlines()[0] == "snapshot_date,value"


def test_native_points_parsed_without_mapping():
    parser = get_parser("native_bank")
    points, _ = parse_bank_points(NATIVE_CSV, parser.effective_options({}))
    assert [p.snapshot_date for p in points] == [date(2024, 1, 31), date(2024, 2, 29)]
    assert points[0].value == Decimal("12500.00")


def test_native_points_accept_french_dates_and_decimals():
    parser = get_parser("native_bank")
    points, _ = parse_bank_points(NATIVE_FR_CSV, parser.effective_options({}))
    assert [p.snapshot_date for p in points] == [date(2024, 1, 31), date(2024, 2, 29)]
    assert points[0].value == Decimal("12500.00")
    assert points[1].value == Decimal("13200.50")


def test_native_missing_columns_yields_no_points():
    parser = get_parser("native_bank")
    points, _ = parse_bank_points("foo,bar\n1,2\n", parser.effective_options({}))
    assert points == []


# ─── The transactional path ──────────────────────────────────────────────
# For the accounts no bank API reaches. Movements, not a balance curve: the
# `delta` mode above integrates them into end-of-day balances, which destroys
# the direction the whole point is to keep.

TRANSACTIONS_CSV = textwrap.dedent("""\
    date,amount,label
    2024-01-15,-42.50,CARTE FNAC
    2024-01-31,1200.00,VIREMENT SALAIRE
    2024-02-03,-850.00,VIREMENT LIVRET A
""")


def test_transaction_parser_is_registered():
    parser = get_parser("generic_bank_transactions")
    assert parser is not None
    assert parser.category.value == "bank"


def test_the_sign_carries_the_direction():
    rows, _ = parse_bank_transactions(TRANSACTIONS_CSV, {})
    assert [(r.day, r.direction, r.amount) for r in rows] == [
        (date(2024, 1, 15), "DBIT", Decimal("42.50")),
        (date(2024, 1, 31), "CRDT", Decimal("1200.00")),
        (date(2024, 2, 3), "DBIT", Decimal("850.00")),
    ]
    assert rows[0].label == "CARTE FNAC"


def test_a_zero_movement_is_not_a_movement():
    """No sign to read, and nothing moved."""
    rows, warnings = parse_bank_transactions("date,amount,label\n2024-01-15,0.00,RIEN\n", {})
    assert rows == []
    assert warnings and "illisible" in warnings[0]


def test_french_dates_and_decimals_are_read():
    rows, _ = parse_bank_transactions(
        "Date;Montant;Libelle\n15/01/2024;-42,50;CARTE FNAC\n",
        {"mapping": {"date": "Date", "amount": "Montant", "label": "Libelle"},
         "date_format": "%d/%m/%Y", "decimal_separator": ","},
    )
    assert [(r.day, r.direction, r.amount) for r in rows] == [
        (date(2024, 1, 15), "DBIT", Decimal("42.50"))
    ]


def test_twin_movements_get_one_reference_each():
    """Same day, same amount, same label — two real coffees, not one row seen
    twice. Without a reference of its own each would collapse into the other."""
    csv_content = "date,amount,label\n2024-01-15,-3.00,CAFE\n2024-01-15,-3.00,CAFE\n"
    rows, _ = parse_bank_transactions(csv_content, {})
    references = [reference for _, reference in _with_references(rows)]
    assert len(set(references)) == 2


def test_the_same_file_yields_the_same_references_whatever_its_order():
    """A statement exported newest-first would otherwise shift every rank and
    re-insert the whole history."""
    reversed_csv = "\n".join(
        [TRANSACTIONS_CSV.splitlines()[0]] + TRANSACTIONS_CSV.splitlines()[:0:-1]
    ) + "\n"
    first, _ = parse_bank_transactions(TRANSACTIONS_CSV, {})
    second, _ = parse_bank_transactions(reversed_csv, {})
    assert [r for _, r in _with_references(first)] == [r for _, r in _with_references(second)]


def test_the_amount_rendering_does_not_change_a_reference():
    """`42.5` and `42.50` are the same movement, and must keep one identity."""
    a, _ = parse_bank_transactions("date,amount,label\n2024-01-15,-42.5,CAFE\n", {})
    b, _ = parse_bank_transactions("date,amount,label\n2024-01-15,-42.50,CAFE\n", {})
    assert _with_references(a)[0][1] == _with_references(b)[0][1]


# ─── Storing them ────────────────────────────────────────────────────────


def _account(session, master_key: str, currency: str = "EUR") -> str:
    return create_bank_account(
        session,
        BankAccountCreate(name="Livret A", balance=Decimal("0"),
                          account_type=BankAccountType.LIVRET_A),
        "import_user", master_key,
    ).id


def _confirm(session, master_key, account_id, rows, parser):
    return parser.execute(
        session, account_id,
        ImportConfirmRequest(account_id=account_id, bank_transactions=rows),
        master_key,
    )


def test_movements_land_in_the_same_table_the_sync_fills(session, master_key):
    parser = get_parser("generic_bank_transactions")
    account_id = _account(session, master_key)
    rows, _ = parse_bank_transactions(TRANSACTIONS_CSV, {})

    result = _confirm(session, master_key, account_id, rows, parser)
    assert result.imported_count == 3

    stored = session.exec(
        select(BankTransaction).where(
            BankTransaction.account_id_bidx == hash_index(account_id, master_key)
        )
    ).all()
    assert len(stored) == 3
    amounts = {decrypt_data(r.amount_enc, master_key) for r in stored}
    assert amounts == {"42.5", "1200", "850"}
    directions = sorted(decrypt_data(r.credit_debit_enc, master_key) for r in stored)
    assert directions == ["CRDT", "DBIT", "DBIT"]


def test_re_importing_the_same_file_changes_nothing(session, master_key):
    """The synthesised reference is what makes level 1 recognise the rows."""
    parser = get_parser("generic_bank_transactions")
    account_id = _account(session, master_key)
    rows, _ = parse_bank_transactions(TRANSACTIONS_CSV, {})

    _confirm(session, master_key, account_id, rows, parser)
    again = _confirm(session, master_key, account_id,
                     parse_bank_transactions(TRANSACTIONS_CSV, {})[0], parser)

    assert again.imported_count == 0
    assert again.skipped_duplicates == 3
    stored = session.exec(
        select(BankTransaction).where(
            BankTransaction.account_id_bidx == hash_index(account_id, master_key)
        )
    ).all()
    assert len(stored) == 3


def test_twin_movements_both_survive_a_re_import(session, master_key):
    """Two identical rows are two movements. The fingerprint alone would keep
    one; the reference keeps both, and re-importing adds neither."""
    parser = get_parser("generic_bank_transactions")
    account_id = _account(session, master_key)
    twins = "date,amount,label\n2024-01-15,-3.00,CAFE\n2024-01-15,-3.00,CAFE\n"

    first = _confirm(session, master_key, account_id, parse_bank_transactions(twins, {})[0], parser)
    assert first.imported_count == 2

    second = _confirm(session, master_key, account_id, parse_bank_transactions(twins, {})[0], parser)
    assert second.imported_count == 0
    stored = session.exec(
        select(BankTransaction).where(
            BankTransaction.account_id_bidx == hash_index(account_id, master_key)
        )
    ).all()
    assert len(stored) == 2


def test_a_second_file_adds_only_what_it_brings(session, master_key):
    parser = get_parser("generic_bank_transactions")
    account_id = _account(session, master_key)
    _confirm(session, master_key, account_id, parse_bank_transactions(TRANSACTIONS_CSV, {})[0], parser)

    extended = TRANSACTIONS_CSV + "2024-02-10,-12.00,CARTE BOULANGERIE\n"
    result = _confirm(session, master_key, account_id,
                      parse_bank_transactions(extended, {})[0], parser)
    assert result.imported_count == 1


def test_a_movement_slotted_between_two_others_shifts_nothing(session, master_key):
    """A row is identified by its own content, not by its position: a late
    export carrying an older movement must not re-insert everything after it."""
    parser = get_parser("generic_bank_transactions")
    account_id = _account(session, master_key)
    _confirm(session, master_key, account_id, parse_bank_transactions(TRANSACTIONS_CSV, {})[0], parser)

    extended = TRANSACTIONS_CSV + "2024-01-20,-15.00,CARTE PRESSE\n"
    result = _confirm(session, master_key, account_id,
                      parse_bank_transactions(extended, {})[0], parser)
    assert result.imported_count == 1
    stored = session.exec(
        select(BankTransaction).where(
            BankTransaction.account_id_bidx == hash_index(account_id, master_key)
        )
    ).all()
    assert len(stored) == 4


def test_the_preview_flags_what_is_already_stored(session, master_key):
    parser = get_parser("generic_bank_transactions")
    account_id = _account(session, master_key)
    _confirm(session, master_key, account_id, parse_bank_transactions(TRANSACTIONS_CSV, {})[0], parser)

    extended = TRANSACTIONS_CSV + "2024-02-10,-12.00,CARTE BOULANGERIE\n"
    preview = parser.preview(session, extended, {}, account_id=account_id, master_key=master_key)
    assert preview.total_rows == 4
    assert preview.duplicates_count == 3
    assert [r.is_duplicate for r in preview.bank_transactions].count(False) == 1


def test_the_transactional_import_writes_no_balance_snapshot(session, master_key):
    """The two bank imports are complementary: a statement does not always
    carry the reference balance a curve would have to be rebuilt from."""
    parser = get_parser("generic_bank_transactions")
    account_id = _account(session, master_key)
    _confirm(session, master_key, account_id, parse_bank_transactions(TRANSACTIONS_CSV, {})[0], parser)

    snapshots = session.exec(
        select(AccountHistory).where(
            AccountHistory.account_id_bidx == hash_index(account_id, master_key)
        )
    ).all()
    assert snapshots == []
