import textwrap
from decimal import Decimal

from services.imports.generic_csv import (
    GenericCryptoParser,
    parse_generic_date,
    parse_generic_decimal,
    parse_stock_rows,
)

FR_STOCK_CSV = textwrap.dedent("""\
    Date;Opération;ISIN;Quantité;Cours;Frais
    15/01/2024;Achat;FR0000120271;10;58,50;1,99
    20/02/2024;Vente;FR0000120271;5;62,10;1,99
    01/03/2024;Dépôt;;500;;
    bad-date;Achat;FR0000120271;10;58,50;1,99
    05/03/2024;Mystère;FR0000120271;10;58,50;
""")

FR_OPTIONS = {
    "mapping": {
        "date": "Date", "type": "Opération", "asset": "ISIN",
        "quantity": "Quantité", "price": "Cours", "fees": "Frais",
    },
    "date_format": "%d/%m/%Y",
    "decimal_separator": ",",
}


def test_parse_generic_decimal():
    assert parse_generic_decimal("1 234,56", ",") == Decimal("1234.56")
    assert parse_generic_decimal("1,234.56", ".") == Decimal("1234.56")
    # Auto-detection
    assert parse_generic_decimal("1.234,56") == Decimal("1234.56")
    assert parse_generic_decimal("1,234.56") == Decimal("1234.56")
    assert parse_generic_decimal("58,50") == Decimal("58.50")
    assert parse_generic_decimal("€ 42.00") == Decimal("42.00")
    assert parse_generic_decimal("") is None


def test_parse_generic_date():
    assert parse_generic_date("2024-01-15").isoformat() == "2024-01-15T00:00:00"
    assert parse_generic_date("15/01/2024").isoformat() == "2024-01-15T00:00:00"
    assert parse_generic_date("15/01/2024", "%d/%m/%Y").isoformat() == "2024-01-15T00:00:00"
    assert parse_generic_date("2024-01-15T10:30:00Z").isoformat() == "2024-01-15T10:30:00"
    assert parse_generic_date("garbage") is None


def test_parse_stock_rows_french_csv():
    rows, warnings = parse_stock_rows(FR_STOCK_CSV, FR_OPTIONS)
    assert len(rows) == 5

    buy = rows[0]
    assert buy.error is None
    assert buy.type == "BUY"
    assert buy.asset_key == "FR0000120271"
    assert buy.amount == 10
    assert buy.price_per_unit == 58.5
    assert buy.fees == 1.99
    assert buy.executed_at == "2024-01-15T00:00:00"

    sell = rows[1]
    assert sell.type == "SELL"

    deposit = rows[2]
    assert deposit.type == "DEPOSIT"
    assert deposit.asset_key == "EUR"
    assert deposit.amount == 500
    assert deposit.price_per_unit == 1

    assert rows[3].error is not None  # bad date
    assert rows[4].error is not None  # unknown type


def test_generic_crypto_builds_eur_leg_from_price():
    csv_content = textwrap.dedent("""\
        Date,Type,Actif,Quantite,PrixUnitaire
        2024-01-15 10:00:00,Achat,BTC,0.01,40000
        2024-02-01 11:00:00,Staking,SOL,0.5,
        2024-03-01 12:00:00,Achat,ETH,0.2,
    """)
    options = {
        "mapping": {"date": "Date", "type": "Type", "asset": "Actif",
                    "quantity": "Quantite", "price": "PrixUnitaire"},
    }
    parser = GenericCryptoParser()
    preview = parser.generate(csv_content, options=options)
    assert preview.total_groups == 3

    # BUY with a price: EUR SPEND counterpart, no manual anchor needed
    g_buy = preview.groups[0]
    types = {r.mapped_type for r in g_buy.rows}
    assert types == {"BUY", "SPEND"}
    spend = next(r for r in g_buy.rows if r.mapped_type == "SPEND")
    assert spend.mapped_asset_key == "EUR"
    assert spend.mapped_amount == 400.0
    assert not g_buy.needs_eur_input

    # REWARD (via 'Staking' alias): no anchor needed
    g_reward = preview.groups[1]
    assert g_reward.rows[0].mapped_type == "REWARD"
    assert not g_reward.needs_eur_input

    # BUY without price: needs manual EUR anchor
    g_no_price = preview.groups[2]
    assert g_no_price.needs_eur_input
