import textwrap

from services.imports.degiro import DegiroParser, parse_degiro
from services.imports.trade_republic import TradeRepublicParser, parse_trade_republic

DEGIRO_TRANSACTIONS_CSV = textwrap.dedent("""\
    Date,Heure,Produit,Code ISIN,Bourse,Lieu d'exécution,Quantité,Cours,,Montant devise locale,,Montant,,Taux de change,Frais de courtage,,Total,,ID Ordre
    15-01-2024,09:05,AIR LIQUIDE,FR0000120073,EPA,EPA,10,"170,50",EUR,"-1705,00",EUR,"-1705,00",EUR,,"-1,00",EUR,"-1706,00",EUR,abc-123
    20-02-2024,14:30,AIR LIQUIDE,FR0000120073,EPA,EPA,-4,"175,00",EUR,"700,00",EUR,"700,00",EUR,,"-1,00",EUR,"699,00",EUR,def-456
""")

DEGIRO_ACCOUNT_CSV = textwrap.dedent("""\
    Date,Heure,Date de valeur,Produit,Code ISIN,Description,FX,Mouvements,,Solde,,ID Ordre
    10-01-2024,10:00,10-01-2024,,,Dépôt,,EUR,"2000,00",EUR,"2000,00",
    05-03-2024,09:00,05-03-2024,AIR LIQUIDE,FR0000120073,Dividende,,EUR,"8,20",EUR,"2008,20",
    06-03-2024,09:00,06-03-2024,AIR LIQUIDE,FR0000120073,Impôts sur dividende,,EUR,"-2,10",EUR,"2006,10",
    07-03-2024,09:00,07-03-2024,,,Retrait,,EUR,"-100,00",EUR,"1906,10",
    15-01-2024,09:05,15-01-2024,AIR LIQUIDE,FR0000120073,Achat 10 @ 170.5 EUR,,EUR,"-1705,00",EUR,"295,00",abc-123
""")

TR_CSV = textwrap.dedent("""\
    Date;Type;ISIN;Name;Shares;Price;Fee;Total
    2024-01-15;Kauf;IE00B4L5Y983;iShares Core MSCI World;2,5;80,00;1,00;201,00
    2024-02-20;Verkauf;IE00B4L5Y983;iShares Core MSCI World;1;85,00;1,00;84,00
    2024-03-01;Dividende;IE00B4L5Y983;iShares Core MSCI World;;;;3,50
    2024-03-10;Einzahlung;;;;;;500,00
""")


def test_degiro_transactions_file():
    rows, warnings = parse_degiro(DEGIRO_TRANSACTIONS_CSV)
    assert len(rows) == 2
    assert not warnings

    buy = rows[0]
    assert buy.type == "BUY"
    assert buy.asset_key == "FR0000120073"
    assert buy.amount == 10
    assert buy.price_per_unit == 170.5
    assert buy.fees == 1.0
    assert buy.executed_at == "2024-01-15T09:05:00"
    assert buy.error is None

    sell = rows[1]
    assert sell.type == "SELL"
    assert sell.amount == 4


def test_degiro_account_file():
    rows, warnings = parse_degiro(DEGIRO_ACCOUNT_CSV)
    # Deposit + dividend + withdrawal; dividend tax and the trade row are skipped
    assert len(rows) == 3
    assert warnings and "ignorée" in warnings[0]

    deposit, dividend, withdraw = rows
    assert deposit.type == "DEPOSIT"
    assert deposit.asset_key == "EUR"
    assert deposit.amount == 2000.0

    assert dividend.type == "DIVIDEND"
    assert dividend.asset_key == "FR0000120073"
    assert dividend.amount == 8.2
    assert dividend.price_per_unit == 1.0

    assert withdraw.type == "WITHDRAW"
    assert withdraw.amount == 100.0


def test_degiro_detect():
    parser = DegiroParser()
    assert parser.detect(DEGIRO_TRANSACTIONS_CSV) >= 0.9
    assert parser.detect(DEGIRO_ACCOUNT_CSV) >= 0.8
    assert parser.detect("a,b,c\n1,2,3") == 0.0


def test_trade_republic_parse():
    rows, _ = parse_trade_republic(TR_CSV, {})
    assert len(rows) == 4

    buy = rows[0]
    assert buy.type == "BUY"
    assert buy.asset_key == "IE00B4L5Y983"
    assert buy.amount == 2.5
    assert buy.price_per_unit == 80.0
    assert buy.fees == 1.0
    assert buy.error is None

    sell = rows[1]
    assert sell.type == "SELL"

    dividend = rows[2]
    assert dividend.type == "DIVIDEND"
    assert dividend.amount == 3.5

    deposit = rows[3]
    assert deposit.type == "DEPOSIT"
    assert deposit.asset_key == "EUR"
    assert deposit.amount == 500.0


def test_trade_republic_detect():
    parser = TradeRepublicParser()
    assert parser.detect(TR_CSV) > 0.5
    assert parser.detect("Date,Montant\n2024-01-01,5") == 0.0
