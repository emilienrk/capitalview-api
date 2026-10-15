import csv
import io
import textwrap
from decimal import Decimal

from services.imports.degiro import DegiroParser, parse_degiro
from services.imports.trade_republic import TradeRepublicParser, parse_trade_republic
from services.imports.trade_republic_crypto import TradeRepublicCryptoParser, generate_preview as tr_crypto_preview

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


# The app's own export (shape of a real 2026 file, cut down). Its cash adds up:
# summed over every line, amount + fee + tax is the account's balance.
TR_EXPORT_CSV = textwrap.dedent("""\
    "datetime","date","account_type","category","type","asset_class","name","symbol","shares","price","amount","fee","tax","currency","original_amount","original_currency","fx_rate","description","transaction_id","counterparty_name","counterparty_iban","payment_reference","mcc_code"
    "2024-02-16T13:14:13.567675Z","2024-02-16","DEFAULT","CASH","CUSTOMER_INBOUND","","Jane DOE","","","","400.000000","","","EUR","","","","Virement","t01","Jane DOE","FR00","",""
    "2024-02-16T14:00:00.000Z","2024-02-16","DEFAULT","TRADING","BUY","STOCK","LVMH","FR0000121014","0.0243100000","822.800000","-20.00","-1.00","-0.06","EUR","","","","","t02","","","",""
    "2024-02-16T16:12:48.738153Z","2024-02-16","DEFAULT","CASH","STOCKPERK","STOCK","Walt Disney","US2546871060","","","9.990000","","","EUR","","","","Stockperk","t03","","","",""
    "2024-02-16T22:59:16.800Z","2024-02-16","DEFAULT","TRADING","BUY","STOCK","Walt Disney","US2546871060","0.0961000000","103.920000","-9.99","","","EUR","","","","","t04","","","",""
    "2024-03-01T00:00:00.000Z","2024-03-01","DEFAULT","CASH","INTEREST_PAYMENT","","","","","","0.070000","","0.00","EUR","","","","Interest payment","t05","","","",""
    "2024-03-26T08:12:00.180Z","2024-03-26","DEFAULT","TRADING","BUY","CRYPTO","Bitcoin","BTC","0.0007630000","65781.040000","-50.19","-1.00","","EUR","","","","","t06","","","",""
    "2024-04-20T10:38:37.628Z","2024-04-20","DEFAULT","TRADING","SELL","CRYPTO","Bitcoin","BTC","-0.0007630000","62910.000000","48.00","-1.00","","EUR","","","","","t07","","","",""
    "2024-04-25T10:00:00.000Z","2024-04-25","DEFAULT","CASH","DIVIDEND","STOCK","LVMH","FR0000121014","0.0243100000","","0.540000","","-0.14","EUR","","","","","t08","","","",""
    "2024-05-16T10:00:00.000Z","2024-05-16","DEFAULT","TRADING","BUY","STOCK","NVIDIA","US67066G1040","0.1137700000","87.890000","-10.00","","","EUR","","","","","t09","","","",""
    "2024-10-08T10:00:00.000Z","2024-10-08","DEFAULT","TRADING","SELL","STOCK","NVIDIA","US67066G1040","-0.1137700000","117.900000","13.41","-1.00","","EUR","","","","","t11","","","",""
    "2024-11-14T10:00:00.000Z","2024-11-14","DEFAULT","CORPORATE_ACTION","INTERMEDIATE_SECURITIES_DISTRIBUTION","SYNTHETIC","ATOS ANR.","FR001400THA4","27.5668640000","","","","","","","","","","t12","","","",""
    "2024-12-12T10:00:00.000Z","2024-12-12","DEFAULT","CORPORATE_ACTION","WORTHLESS","SYNTHETIC","ATOS ANR.","FR001400THA4","-27.5668640000","","","","","","","","","","t13","","","",""
    "2026-01-02T09:13:18.257Z","2026-01-02","DEFAULT","TRADING","BUY","STOCK","AMD","US0079031078","0.1074400000","186.1000000000","-19.99","-1.00","","EUR","","","","","t14","","","",""
    "2026-01-02T09:13:18.812337Z","2026-01-02","DEFAULT","CASH","GIFT","","","","","","21.000000","","","EUR","","","","Gift lottery order","t15","","","",""
    "2026-01-26T10:00:00.000Z","2026-01-26","DEFAULT","DELIVERY","MIGRATION","STOCK","AMD","US0079031078","-0.1074400000","195.3649000000","","","","EUR","","","","","t16","","","",""
    "2026-01-26T10:00:00.000Z","2026-01-26","DEFAULT","DELIVERY","MIGRATION","STOCK","AMD","US0079031078","0.1074400000","195.3649000000","","","","EUR","","","","","t17","","","",""
    "2026-06-08T14:04:54.096008Z","2026-06-08","DEFAULT","CASH","IPO_SUBSCRIPTION","STOCK","SpaceX","US84615Q1031","","","-50.000000","-1.00","","EUR","","","","IPO pre-payment","t18","","","",""
    "2026-06-12T12:17:23.991Z","2026-06-12","DEFAULT","TRADING","BUY","STOCK","SpaceX","US84615Q1031","0.0553780000","116.7628060000","","","","EUR","","","","","t19","","","",""
    "2026-06-12T12:17:24.255248Z","2026-06-12","DEFAULT","CASH","IPO_SUBSCRIPTION","STOCK","SpaceX","US84615Q1031","","","43.530000","","","EUR","","","","partial reimbursement","t20","","","",""
    "2026-06-22T07:18:13.217Z","2026-06-22","DEFAULT","CASH","CUSTOMER_OUTBOUND_REQUEST","","Jane DOE","","","","-200.000000","","","EUR","","","","","t21","Jane DOE","FR00","",""
    "2026-06-23T07:18:13.217Z","2026-06-23","DEFAULT","CASH","CARD_TRANSACTION","","Shop","","","","-20.000000","","","EUR","","","","TR Card Transaction","t22","","","","6012"
""")


def _tr_export_cash(csv_content: str) -> Decimal:
    reader = csv.DictReader(io.StringIO(csv_content))
    return sum(
        (Decimal(line[col] or "0") for line in reader for col in ("amount", "fee", "tax")),
        Decimal("0"),
    )


def _replayed_cash(rows) -> Decimal:
    cash = Decimal("0")
    for r in rows:
        amount, price, fees = (Decimal(str(v)) for v in (r.amount, r.price_per_unit, r.fees))
        cash += {
            "DEPOSIT": amount - fees, "WITHDRAW": -amount - fees, "BUY": -amount * price - fees,
            "SELL": amount * price - fees, "DIVIDEND": amount * price - fees,
        }[r.type]
    return cash


def test_trade_republic_export_detected_on_both_sides():
    assert TradeRepublicParser().detect(TR_EXPORT_CSV) >= 0.9
    assert TradeRepublicCryptoParser().detect(TR_EXPORT_CSV) >= 0.9
    assert TradeRepublicCryptoParser().detect(TR_CSV) == 0.0


def test_trade_republic_export_keeps_the_cash_balance():
    rows, _ = parse_trade_republic(TR_EXPORT_CSV, {})

    assert not [r.error for r in rows if r.error]
    assert not [r for r in rows if r.needs_asset_key]
    assert abs(_replayed_cash(rows) - _tr_export_cash(TR_EXPORT_CSV)) < Decimal("0.01")


def test_trade_republic_export_maps_each_movement():
    rows, warnings = parse_trade_republic(TR_EXPORT_CSV, {})
    by_tx = {(r.type, r.asset_key, r.notes): r for r in rows}

    lvmh = next(r for r in rows if r.type == "BUY" and r.asset_key == "FR0000121014")
    assert lvmh.fees == 1.06  # broker fee + the French transaction tax
    assert lvmh.amount * lvmh.price_per_unit <= 20.0

    dividend = next(r for r in rows if r.type == "DIVIDEND" and r.asset_key == "FR0000121014")
    assert (dividend.amount, dividend.fees) == (0.54, 0.14)

    # Interest, gifts and offered shares are income on the cash, not deposits.
    income = [r for r in rows if r.type == "DIVIDEND" and r.asset_key == "EUR"]
    assert sorted(r.amount for r in income) == [0.07, 9.99, 21.0]

    # Crypto trades only move cash out to the crypto account and back.
    assert by_tx[("WITHDRAW", "EUR", "Achat Bitcoin (compte crypto)")].amount == 51.19
    assert by_tx[("DEPOSIT", "EUR", "Vente Bitcoin (compte crypto)")].amount == 47.0
    assert not [r for r in rows if r.asset_key == "BTC"]

    spacex = next(r for r in rows if r.asset_key == "US84615Q1031")
    assert spacex.fees == 1.0  # the pre-payment's fee, carried onto the allotment

    assert [r.type for r in rows if r.asset_key == "EUR" and r.notes is None] == ["DEPOSIT", "WITHDRAW", "WITHDRAW"]
    assert not [r for r in rows if r.asset_key == "FR001400THA4"]
    assert len(warnings) == 4


def test_trade_republic_gift_is_dated_with_the_purchase_it_pays_for():
    rows, _ = parse_trade_republic(TR_EXPORT_CSV, {})
    amd = next(r for r in rows if r.type == "BUY" and r.asset_key == "US0079031078")
    gift = next(r for r in rows if r.notes == "Cadeau Trade Republic")
    assert gift.executed_at == amd.executed_at


def test_trade_republic_export_unknown_type_is_an_error():
    csv_content = TR_EXPORT_CSV + (
        '"2026-07-01T00:00:00Z","2026-07-01","DEFAULT","CASH","SOMETHING_NEW","","","","","","5.00","","","EUR","","","","","t99","","","",""\n'
    )
    rows, _ = parse_trade_republic(csv_content, {})
    assert [r.error for r in rows if r.error] == ["Type d'opération inconnu: « SOMETHING_NEW »"]


def test_trade_republic_crypto_mirrors_the_stock_account_cash():
    preview = tr_crypto_preview(TR_EXPORT_CSV)

    assert preview.total_groups == 4
    assert preview.groups_needing_eur == 0
    funded, bought, sold, paid_out = preview.groups
    # The euros arrive from the stock account and go back to it, as it records.
    assert [(r.mapped_type, r.mapped_amount) for r in funded.rows] == [("DEPOSIT", 51.19)]
    assert [(r.mapped_type, r.mapped_amount) for r in paid_out.rows] == [("WITHDRAW", 47.0)]
    assert [(r.mapped_type, r.mapped_asset_key, r.mapped_amount) for r in bought.rows] == [
        ("BUY", "BTC", 0.000763), ("SPEND", "EUR", 51.19),
    ]
    assert [(r.mapped_type, r.mapped_asset_key, r.mapped_amount) for r in sold.rows] == [
        ("DEPOSIT", "EUR", 47.0), ("SPEND", "BTC", 0.000763),
    ]


def test_trade_republic_crypto_line_is_read_by_its_cash_not_its_label():
    csv_content = TR_EXPORT_CSV + (
        '"2026-07-01T00:00:00Z","2026-07-01","DEFAULT","TRADING","SAVINGS_PLAN","CRYPTO","Bitcoin","BTC","0.0001","","-10.00","","","EUR","","","","","t97","","","",""\n'
        '"2026-07-02T00:00:00Z","2026-07-02","DEFAULT","DELIVERY","TRANSFER_OUT","CRYPTO","Bitcoin","BTC","-0.0001","","","","","","","","","","t98","","","",""\n'
    )
    rows, warnings = parse_trade_republic(csv_content, {})
    plan = [r for r in rows if r.executed_at.startswith("2026-07-01")]
    assert [(r.type, r.amount) for r in plan] == [("WITHDRAW", 10.0)]
    assert not [r for r in rows if r.executed_at.startswith("2026-07-02")]
    assert any("crypto" in w for w in warnings)


def test_trade_republic_split_is_left_to_the_file():
    csv_content = TR_EXPORT_CSV + (
        '"2026-07-03T00:00:00Z","2026-07-03","DEFAULT","CORPORATE_ACTION","SPLIT","STOCK","NVIDIA","US67066G1040","0.9","","","","","","","","","SPLIT","t96","","","",""\n'
    )
    rows, _ = parse_trade_republic(csv_content, {})
    assert [r.error.split(" :")[0] for r in rows if r.error] == ["Division d'actions non prise en charge"]
