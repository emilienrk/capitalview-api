import textwrap
from datetime import date
from decimal import Decimal

from services.imports.bank_csv import parse_bank_points

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
