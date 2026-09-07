import textwrap
from datetime import date
from decimal import Decimal

from sqlmodel import select

from dtos.bank import BankAccountCreate
from dtos.imports import ImportConfirmRequest
from models.account_history import AccountHistory
from models.banking import BankTransaction
from models.enums import BankAccountType
from dtos.bank import BankHistoryEntry
from models.bank import BankAccount
from services.bank import (
    create_bank_account,
    get_bank_account_history,
    import_bank_account_history,
)
from services.encryption import decrypt_data, hash_index

from services.imports.bank_csv import (
    _with_references,
    parse_bank_points,
    parse_bank_transactions,
)
from services.imports.registry import get_parser, list_parsers

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


def test_native_parser_is_still_resolvable_but_no_longer_offered():
    """Old files and saved imports still name it; nothing proposes it."""
    assert get_parser("native_bank") is not None
    assert "native_bank" not in {s.source_id for s in list_parsers()}


def test_generic_parser_detects_the_documented_header():
    parser = get_parser("generic_bank")
    assert parser.detect(NATIVE_CSV) == 1.0
    assert parser.detect(NATIVE_FR_CSV) == 1.0  # any delimiter
    assert parser.detect("Date;Solde\n15/01/2024;1000,00\n") == 0.0


def test_native_parser_no_longer_competes_on_detection():
    """Both scoring 1.0 would make the winner a coin toss."""
    assert get_parser("native_bank").detect(NATIVE_CSV) == 0.0


def test_generic_parser_reads_the_documented_shape_without_a_mapping():
    parser = get_parser("generic_bank")
    points, _ = parse_bank_points(NATIVE_CSV, parser.effective_options({}))
    assert [p.snapshot_date for p in points] == [date(2024, 1, 31), date(2024, 2, 29)]
    assert points[0].value == Decimal("12500.00")


def test_generic_parser_still_honours_a_mapping():
    parser = get_parser("generic_bank")
    options = parser.effective_options({
        "mapping": {"date": "Date", "balance": "Solde"},
        "date_format": "%d/%m/%Y",
        "decimal_separator": ",",
    })
    points, _ = parse_bank_points(BALANCE_CSV, options)
    assert [p.value for p in points] == [Decimal("1050.00"), Decimal("980.50")]


def test_balance_parsers_offer_a_template():
    for source_id in ("generic_bank", "native_bank"):
        parser = get_parser(source_id)
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


def test_transaction_parser_detects_its_own_header():
    parser = get_parser("generic_bank_transactions")
    assert parser.detect(TRANSACTIONS_CSV) == 1.0
    # `snapshot_date` is not a `date` column: the two shapes stay apart.
    assert parser.detect(NATIVE_CSV) == 0.0


def test_default_mappings_are_published():
    """What the UI reads to know a file needs no mapping step."""
    sources = {s.source_id: s for s in list_parsers()}
    assert sources["generic_bank"].default_mapping == {"date": "snapshot_date", "balance": "value"}
    assert sources["generic_bank_transactions"].default_mapping == {
        "date": "date", "amount": "amount", "label": "label",
    }


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


def _confirm(session, master_key, account_id, rows, parser, options=None):
    return parser.execute(
        session, account_id,
        ImportConfirmRequest(account_id=account_id, bank_transactions=rows, options=options or {}),
        master_key,
    )


def _curve(session, master_key, account_id):
    """The account's stored curve as {date: value}."""
    return {
        s.snapshot_date: s.total_value
        for s in get_bank_account_history(session, account_id, master_key)
    }


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


# ─── The curve the movements describe ────────────────────────────────────
# A statement carries no balance of its own, so the curve is anchored on the
# balance held before its first line — zero unless the user says otherwise.


def test_the_curve_runs_one_point_a_day_from_the_first_movement(session, master_key):
    parser = get_parser("generic_bank_transactions")
    account_id = _account(session, master_key)
    rows, _ = parse_bank_transactions(TRANSACTIONS_CSV, {})

    _confirm(session, master_key, account_id, rows, parser, {"initial_balance": "2000"})
    curve = _curve(session, master_key, account_id)

    # 15/01 → 03/02, every day, nothing before the first movement.
    assert min(curve) == date(2024, 1, 15)
    assert max(curve) == date(2024, 2, 3)
    assert len(curve) == 20
    assert curve[date(2024, 1, 15)] == Decimal("1957.50")   # 2000 - 42.50
    assert curve[date(2024, 1, 20)] == Decimal("1957.50")   # no movement: carried
    assert curve[date(2024, 1, 31)] == Decimal("3157.50")   # + 1200
    assert curve[date(2024, 2, 3)] == Decimal("2307.50")    # - 850


def test_the_anchor_shifts_the_whole_curve(session, master_key):
    parser = get_parser("generic_bank_transactions")
    rows, _ = parse_bank_transactions(TRANSACTIONS_CSV, {})

    from_zero = _account(session, master_key)
    _confirm(session, master_key, from_zero, rows, parser)
    anchored = _account(session, master_key)
    _confirm(session, master_key, anchored, rows, parser, {"initial_balance": "2000"})

    zero_curve, shifted = _curve(session, master_key, from_zero), _curve(session, master_key, anchored)
    assert all(shifted[d] - zero_curve[d] == Decimal("2000") for d in zero_curve)


def test_the_balance_lands_on_what_the_movements_end_at(session, master_key):
    parser = get_parser("generic_bank_transactions")
    account_id = _account(session, master_key)
    rows, _ = parse_bank_transactions(TRANSACTIONS_CSV, {})

    _confirm(session, master_key, account_id, rows, parser, {"initial_balance": "2000"})

    account = session.get(BankAccount, account_id)
    assert Decimal(decrypt_data(account.balance_enc, master_key)) == Decimal("2307.50")
    # The movements already hold what the linked cashflows would have added.
    assert account.balance_updated_at == date.today()


def test_a_dip_below_zero_is_reported_not_refused(session, master_key):
    """The usual sign of an anchor left at zero on an account that had money."""
    parser = get_parser("generic_bank_transactions")
    account_id = _account(session, master_key)

    preview = parser.preview(session, TRANSACTIONS_CSV, {}, account_id=account_id, master_key=master_key)

    assert preview.bank_curve.opening_balance == Decimal("0")
    assert preview.bank_curve.closing_balance == Decimal("307.50")
    assert preview.bank_curve.first_negative_date == date(2024, 1, 15)
    assert any("sous zéro" in w for w in preview.warnings)

    priced = parser.preview(session, TRANSACTIONS_CSV, {"initial_balance": "2000"},
                            account_id=account_id, master_key=master_key)
    assert priced.bank_curve.first_negative_date is None
    assert not priced.warnings


def test_an_older_statement_does_not_walk_the_balance_back(session, master_key):
    """It rebuilds its own stretch of the curve, but says nothing about today."""
    parser = get_parser("generic_bank_transactions")
    account_id = _account(session, master_key)
    account = session.get(BankAccount, account_id)
    import_bank_account_history(
        session, account,
        [BankHistoryEntry(snapshot_date=date(2025, 6, 1), value=Decimal("9000"))],
        master_key,
    )

    rows, _ = parse_bank_transactions(TRANSACTIONS_CSV, {})
    _confirm(session, master_key, account_id, rows, parser, {"initial_balance": "2000"})

    session.refresh(account)
    assert Decimal(decrypt_data(account.balance_enc, master_key)) == Decimal("0")  # untouched
    curve = _curve(session, master_key, account_id)
    assert curve[date(2024, 2, 3)] == Decimal("2307.50")  # its own window, rebuilt
    assert curve[date(2025, 6, 1)] == Decimal("9000")     # and the later truth kept
