import textwrap
from datetime import date, timedelta
from decimal import Decimal

from sqlmodel import select

from dtos.bank import BankAccountCreate, BankEntryRequest
from dtos.imports import ImportConfirmRequest
from models.account_history import AccountHistory
from models.banking import BankTransaction
from models.enums import BankAccountType
from dtos.bank import BankHistoryEntry
from models.bank import BankAccount
from services.bank import create_bank_account, get_bank_account_history
from services.bank_ledger import add_entry
from services.encryption import decrypt_data, hash_index

from services.imports.bank_csv import _with_references, parse_bank_transactions
from services.imports.registry import get_parser, list_parsers

def test_balance_files_are_no_longer_offered():
    """A balance is never typed nor imported: the operations make it."""
    assert {s.source_id for s in list_parsers() if s.category == "bank"} == {"generic_bank_transactions"}


# ─── The transactional path ──────────────────────────────────────────────

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
    assert parser.detect("snapshot_date,value\n2024-01-31,12500.00\n") == 0.0


def test_default_mappings_are_published():
    """What the UI reads to know a file needs no mapping step."""
    sources = {s.source_id: s for s in list_parsers()}
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
        BankAccountCreate(name="Livret A", account_type=BankAccountType.LIVRET_A),
        "import_user", master_key,
    ).id


def _confirm(session, master_key, account_id, rows, parser, options=None):
    return parser.execute(
        session, account_id,
        ImportConfirmRequest(account_id=account_id, bank_transactions=rows, options=options or {}),
        master_key,
    )


def _open_with(session, master_key, account_id, amount="2000", day=date(2024, 1, 14)):
    """Money already on the account: its first operation."""
    add_entry(session, session.get(BankAccount, account_id),
              BankEntryRequest(day=day, amount=Decimal(amount), label="Solde de départ"), master_key)


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


def test_the_curve_runs_one_point_a_day_from_the_first_operation(session, master_key):
    parser = get_parser("generic_bank_transactions")
    account_id = _account(session, master_key)
    _open_with(session, master_key, account_id)
    rows, _ = parse_bank_transactions(TRANSACTIONS_CSV, {})

    _confirm(session, master_key, account_id, rows, parser)
    curve = _curve(session, master_key, account_id)

    assert min(curve) == date(2024, 1, 14)
    assert max(curve) == date.today() - timedelta(days=1)
    assert curve[date(2024, 1, 14)] == Decimal("2000.00")
    assert curve[date(2024, 1, 15)] == Decimal("1957.50")   # 2000 - 42.50
    assert curve[date(2024, 1, 20)] == Decimal("1957.50")   # no movement: carried
    assert curve[date(2024, 1, 31)] == Decimal("3157.50")   # + 1200
    assert curve[date(2024, 2, 3)] == Decimal("2307.50")    # - 850
    assert curve[max(curve)] == Decimal("2307.50")


def test_the_balance_lands_on_what_the_movements_end_at(session, master_key):
    parser = get_parser("generic_bank_transactions")
    account_id = _account(session, master_key)
    _open_with(session, master_key, account_id)
    rows, _ = parse_bank_transactions(TRANSACTIONS_CSV, {})

    _confirm(session, master_key, account_id, rows, parser)

    account = session.get(BankAccount, account_id)
    assert Decimal(decrypt_data(account.balance_enc, master_key)) == Decimal("2307.50")
    # The movements already hold what the linked cashflows would have added.
    assert account.balance_updated_at == date.today()


def test_a_dip_below_zero_is_reported_not_refused(session, master_key):
    """The usual sign of the money already on the account left out."""
    parser = get_parser("generic_bank_transactions")
    account_id = _account(session, master_key)

    preview = parser.preview(session, TRANSACTIONS_CSV, {}, account_id=account_id, master_key=master_key)
    assert any("sous zéro" in w for w in preview.warnings)

    _open_with(session, master_key, account_id)
    opened = parser.preview(session, TRANSACTIONS_CSV, {}, account_id=account_id, master_key=master_key)
    assert not opened.warnings


def test_the_next_month_picks_up_where_the_operations_left_off(session, master_key):
    parser = get_parser("generic_bank_transactions")
    account_id = _account(session, master_key)
    _open_with(session, master_key, account_id)
    _confirm(session, master_key, account_id, parse_bank_transactions(TRANSACTIONS_CSV, {})[0], parser)

    february = "date,amount,label\n2024-02-20,-100.00,CARTE\n2024-02-25,300.00,VIREMENT\n"
    preview = parser.preview(session, february, {}, account_id=account_id, master_key=master_key)
    assert preview.bank_balance_after == Decimal("2507.50")
    assert not preview.warnings

    _confirm(session, master_key, account_id, parse_bank_transactions(february, {})[0], parser)
    curve = _curve(session, master_key, account_id)
    assert curve[date(2024, 2, 20)] == Decimal("2207.50")
    assert curve[date(2024, 2, 25)] == Decimal("2507.50")


def test_an_older_statement_still_counts_in_the_balance(session, master_key):
    """The operations are the account's truth, whatever their age: an older
    statement adds to the balance instead of being drawn on its own stretch."""
    parser = get_parser("generic_bank_transactions")
    account_id = _account(session, master_key)
    later, _ = parse_bank_transactions("date,amount,label\n2025-06-01,100.00,VIREMENT\n", {})
    _confirm(session, master_key, account_id, later, parser)

    rows, _ = parse_bank_transactions(TRANSACTIONS_CSV, {})
    _confirm(session, master_key, account_id, rows, parser)

    account = session.get(BankAccount, account_id)
    assert Decimal(decrypt_data(account.balance_enc, master_key)) == Decimal("407.50")
    curve = _curve(session, master_key, account_id)
    assert curve[date(2024, 2, 3)] == Decimal("307.50")
    assert curve[date(2025, 6, 1)] == Decimal("407.50")


# ─── On an account the bank already feeds ────────────────────────────────

BANK_HISTORY_FROM = date(2024, 3, 1)

# Opens a month before the bank's history and runs into it.
STATEMENT_INTO_THE_BANK_CSV = textwrap.dedent("""\
    date,amount,label
    2024-02-10,500.00,VIREMENT
    2024-02-20,-30.00,CARTE SPAR
    2024-03-05,-40.00,CARTE FNAC
""")


def _link(session, master_key, account_id, served_from=BANK_HISTORY_FROM):
    from models.banking import BankAccountLink, BankSession
    from services.encryption import encrypt_data

    bank_session = BankSession(
        user_uuid_bidx=hash_index("import_user", master_key),
        session_id_enc=encrypt_data("eb-session", master_key),
        status="AUTHORIZED",
        consent_valid_until=date.today(),
        authorized_at=date.today(),
    )
    session.add(bank_session)
    session.commit()
    session.add(BankAccountLink(
        user_uuid_bidx=hash_index("import_user", master_key),
        bank_account_uuid_bidx=hash_index(account_id, master_key),
        session_uuid=bank_session.uuid,
        identification_hash_bidx=hash_index("ident", master_key),
        account_uid_enc=encrypt_data("uid", master_key),
        anchor_date=date.today(),
        anchor_balance_enc=encrypt_data("0", master_key),
        last_synced_at=date.today(),
        history_seeded=served_from is not None,
        history_served_from_enc=encrypt_data(served_from.isoformat(), master_key) if served_from else None,
    ))
    session.commit()


def _bank_row(reference, booked, amount, transaction_date=None):
    from services.banking.transactions import STATUS_BOOKED

    return {
        "entry_reference": reference,
        "transaction_amount": {"currency": "EUR", "amount": str(abs(Decimal(amount)))},
        "credit_debit_indicator": "CRDT" if Decimal(amount) > 0 else "DBIT",
        "status": STATUS_BOOKED,
        "booking_date": booked.isoformat(),
        "transaction_date": (transaction_date or booked).isoformat(),
    }


def _synced_account(session, master_key, bank_rows=None):
    """A linked account whose history opens on BANK_HISTORY_FROM at 480 €, after
    a 20 € card payment that day: the eve of it stood at 500 €."""
    from services.bank import replace_history_window
    from services.banking.transactions import store_transactions

    account_id = _account(session, master_key)
    _link(session, master_key, account_id)
    store_transactions(session, master_key, account_id, bank_rows or [
        _bank_row("bank-1", BANK_HISTORY_FROM, "-20"),
        _bank_row("bank-2", date(2024, 3, 5), "-40", transaction_date=date(2024, 3, 5)),
    ])
    replace_history_window(
        session, session.get(BankAccount, account_id),
        [BankHistoryEntry(snapshot_date=BANK_HISTORY_FROM, value=Decimal("480"))],
        master_key, BANK_HISTORY_FROM, BANK_HISTORY_FROM,
    )
    return account_id


def _stored_count(session, master_key, account_id):
    return len(session.exec(
        select(BankTransaction).where(BankTransaction.account_id_bidx == hash_index(account_id, master_key))
    ).all())


def test_a_linked_account_takes_only_the_days_before_the_bank(session, master_key):
    parser = get_parser("generic_bank_transactions")
    account_id = _synced_account(session, master_key)

    preview = parser.preview(session, STATEMENT_INTO_THE_BANK_CSV, {},
                             account_id=account_id, master_key=master_key)
    assert preview.bank_history_from == BANK_HISTORY_FROM
    assert preview.covered_by_bank_count == 1
    assert [r.day for r in preview.bank_transactions] == [date(2024, 2, 10), date(2024, 2, 20)]

    result = _confirm(session, master_key, account_id, preview.bank_transactions
                      + parse_bank_transactions(STATEMENT_INTO_THE_BANK_CSV, {})[0][2:], parser)
    assert result.imported_count == 2
    assert result.covered_by_bank_count == 1  # re-filtered, whatever the client sent
    assert _stored_count(session, master_key, account_id) == 4


def test_the_curve_meets_the_bank_on_the_eve_of_its_history(session, master_key):
    parser = get_parser("generic_bank_transactions")
    account_id = _synced_account(session, master_key)
    account = session.get(BankAccount, account_id)
    balance_before = decrypt_data(account.balance_enc, master_key)

    preview = parser.preview(session, STATEMENT_INTO_THE_BANK_CSV, {},
                             account_id=account_id, master_key=master_key)
    # 500 € on the eve, reached through +500 then −30: the account opened at 30 €.
    assert preview.bank_curve.opening_balance == Decimal("30")
    assert preview.bank_curve.end_date == date(2024, 2, 29)
    assert preview.bank_curve.closing_balance == Decimal("500")

    _confirm(session, master_key, account_id, preview.bank_transactions, parser)
    curve = _curve(session, master_key, account_id)
    assert curve[date(2024, 2, 10)] == Decimal("530")
    assert curve[date(2024, 2, 29)] == Decimal("500")
    assert curve[BANK_HISTORY_FROM] == Decimal("480")  # the bank's own, untouched

    session.refresh(account)
    assert decrypt_data(account.balance_enc, master_key) == balance_before


def test_a_payment_the_bank_booked_after_its_history_opened_is_not_imported_twice(session, master_key):
    """Made on the 28th, booked on the 1st: the statement dates it before the
    bank's history, the bank inside it. One bank movement claims one row only."""
    parser = get_parser("generic_bank_transactions")
    account_id = _synced_account(session, master_key, bank_rows=[
        _bank_row("bank-1", BANK_HISTORY_FROM, "-1.70", transaction_date=date(2024, 2, 28)),
    ])
    csv = textwrap.dedent("""\
        date,amount,label
        2024-02-27,-1.70,Burger King
        2024-02-28,-1.70,Burger King
        2024-02-28,-3.01,SPAR
    """)

    preview = parser.preview(session, csv, {}, account_id=account_id, master_key=master_key)
    assert preview.covered_by_bank_count == 1
    assert sorted((r.day, r.amount) for r in preview.bank_transactions) == [
        (date(2024, 2, 27), Decimal("1.70")),
        (date(2024, 2, 28), Decimal("3.01")),
    ]


def test_re_importing_on_a_linked_account_keeps_the_same_cut(session, master_key):
    """The file's own rows, once stored, must not pass for the bank's history."""
    parser = get_parser("generic_bank_transactions")
    account_id = _synced_account(session, master_key)
    rows, _ = parse_bank_transactions(STATEMENT_INTO_THE_BANK_CSV, {})
    _confirm(session, master_key, account_id, rows, parser)

    preview = parser.preview(session, STATEMENT_INTO_THE_BANK_CSV, {},
                             account_id=account_id, master_key=master_key)
    assert preview.bank_history_from == BANK_HISTORY_FROM
    assert preview.duplicates_count == 2
    assert _confirm(session, master_key, account_id, rows, parser).imported_count == 0


def test_movements_from_an_export_older_than_the_seeding_move_the_cut_back(session, master_key):
    parser = get_parser("generic_bank_transactions")
    account_id = _synced_account(session, master_key, bank_rows=[
        _bank_row("export-1", date(2024, 2, 15), "-30"),
    ])

    preview = parser.preview(session, STATEMENT_INTO_THE_BANK_CSV, {},
                             account_id=account_id, master_key=master_key)
    assert preview.bank_history_from == date(2024, 2, 15)
    assert [r.day for r in preview.bank_transactions] == [date(2024, 2, 10)]
