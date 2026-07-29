import textwrap
from datetime import date
from decimal import Decimal

from services.imports.bank_csv import parse_bank_points
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
