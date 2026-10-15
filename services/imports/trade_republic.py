"""
Trade Republic CSV import (stock side).

Two shapes are read:

- **The app's transaction export** (Profil › Exporter les transactions):
  ``datetime,date,account_type,category,type,asset_class,name,symbol,shares,
  price,amount,fee,tax,currency,…``. One line per movement of the account's
  cash or securities, amounts signed, ``symbol`` = ISIN for securities and
  ticker for crypto. ``amount`` excludes ``fee`` and ``tax``: summed over the
  file, the three give the account's cash balance.
- **Older community exports** (FR/EN/DE column aliases): Date/Datum, Type/Typ,
  ISIN, Name/Nom, Shares/Quantité/Anzahl, Price/Prix/Preis, Fee/Frais/Gebühr,
  Total/Montant/Betrag.

Crypto bought on Trade Republic belongs to a crypto account
(``trade_republic_crypto`` reads the same file): here its trades only move the
cash out to that account and back, so the stock account's balance stays right.

asset_key = ISIN. If the file matches neither shape, ``generic_stock`` (manual
column mapping) is the fallback.
"""

from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal

from dtos.imports import StockImportRowPreview
from services.imports.base import csv_header_line, header_has
from services.imports.generic_csv import (
    StockImportParser,
    map_type,
    parse_generic_date,
    parse_generic_decimal,
    read_rows,
)
from services.imports.registry import register

# ── The app's transaction export ─────────────────────────────

EXPORT_COLUMNS = ("datetime", "category", "type", "asset_class", "symbol", "shares", "amount")

_CASH_IN = {"CUSTOMER_INBOUND", "TRANSFER_INBOUND", "TRANSFER_INSTANT_INBOUND"}
_CASH_OUT = {
    "CUSTOMER_OUTBOUND_REQUEST", "TRANSFER_OUTBOUND", "TRANSFER_INSTANT_OUTBOUND",
    "CARD_TRANSACTION",
}
# Cash credited without the user paying it in: income, never a contribution.
_INCOME = {
    "INTEREST_PAYMENT": "Intérêts",
    "GIFT": "Cadeau Trade Republic",
    "STOCKPERK": "Action offerte",
}
# Each is either cancelled out by another line of the file or carried by one:
# a migration is a −x/+x pair, an IPO's pre-payment and refund add up to the
# BUY that follows, and distributed rights are worth nothing until sold.
_IGNORED = {
    "MIGRATION": "transferts de titres internes à Trade Republic",
    "IPO_SUBSCRIPTION": "acomptes d'introduction en bourse, repris par l'achat qui suit",
    "INTERMEDIATE_SECURITIES_DISTRIBUTION": "droits attribués sans coût",
    "CRYPTO": "mouvements crypto sans euros, repris par l'import Crypto",
}

# The price is derived from the cash amount and rounded down, so that a
# purchase never costs a fraction of a cent more than the cash the file shows:
# the app would otherwise auto-provision the shortfall.
_PRICE_STEP = Decimal("0.000001")
_FUNDING_LAG = timedelta(seconds=5)


def is_export(csv_content: str) -> bool:
    return header_has(csv_content, *EXPORT_COLUMNS)


def read_export(csv_content: str) -> list[dict]:
    """The export's lines, oldest first."""
    lines, _ = read_rows(csv_content, {"delimiter": ","})
    return sorted(lines, key=lambda line: (line.get("datetime") or "").strip())


def cell(line: dict, column: str) -> str:
    return (line.get(column) or "").strip()


def decimal_cell(line: dict, column: str) -> Decimal:
    return parse_generic_decimal(cell(line, column), ".") or Decimal("0")


def _parse_export(csv_content: str) -> tuple[list[StockImportRowPreview], list[str]]:
    rows: list[StockImportRowPreview] = []
    ignored: dict[str, int] = {}
    held: dict[str, Decimal] = {}
    ipo_fees: dict[str, Decimal] = {}
    last_buy_at: datetime | None = None

    for i, line in enumerate(read_export(csv_content)):
        raw_type = cell(line, "type")
        tx_type = raw_type.upper()
        isin = cell(line, "symbol").upper()
        name = cell(line, "name") or None
        executed_at = parse_generic_date(cell(line, "datetime") or cell(line, "date"))
        shares = decimal_cell(line, "shares")
        amount = decimal_cell(line, "amount")
        fees = abs(decimal_cell(line, "fee")) + abs(decimal_cell(line, "tax"))
        when = executed_at.isoformat() if executed_at else cell(line, "date")

        def row(kind: str, quantity: Decimal, price: Decimal, *, asset_key: str | None,
                row_fees: Decimal = Decimal("0"), notes: str | None = None,
                error: str | None = None) -> None:
            rows.append(StockImportRowPreview(
                row_index=i,
                executed_at=when,
                type=kind,
                asset_key=asset_key,
                isin=asset_key if asset_key and asset_key != "EUR" else None,
                name=name if asset_key != "EUR" else None,
                amount=float(quantity),
                price_per_unit=float(price),
                fees=float(row_fees),
                notes=notes,
                error=error or (f"Date invalide: « {cell(line, 'datetime')} »" if executed_at is None else None),
            ))

        if tx_type in _IGNORED:
            ignored[tx_type] = ignored.get(tx_type, 0) + 1
            if tx_type == "IPO_SUBSCRIPTION" and amount < 0:
                ipo_fees[isin] = ipo_fees.get(isin, Decimal("0")) + fees
            continue

        if cell(line, "asset_class").upper() == "CRYPTO":
            # Cash sent to, or received from, the crypto account holding the
            # coins, whatever the line's label; a line moving no cash stays there.
            cash = amount - fees
            if not cash:
                ignored["CRYPTO"] = ignored.get("CRYPTO", 0) + 1
                continue
            label = f"{'Achat' if shares > 0 else 'Vente'} {name or isin} (compte crypto)"
            row("WITHDRAW" if cash < 0 else "DEPOSIT", abs(cash), Decimal("1"),
                asset_key="EUR", notes=label)
            continue

        if tx_type in _CASH_IN or tx_type in _CASH_OUT:
            row("DEPOSIT" if tx_type in _CASH_IN else "WITHDRAW", abs(amount), Decimal("1"),
                asset_key="EUR",
                error=None if amount else "Montant invalide")
            continue

        if tx_type in _INCOME:
            # A lottery gift lands a second after the purchase of the share it
            # pays for: dated with it, it is replayed first.
            if executed_at and last_buy_at and timedelta(0) <= executed_at - last_buy_at <= _FUNDING_LAG:
                when = last_buy_at.isoformat()
            row("DIVIDEND", abs(amount), Decimal("1"), asset_key="EUR", row_fees=fees,
                notes=f"{_INCOME[tx_type]}{f' — {name}' if name and tx_type == 'STOCKPERK' else ''}")
            continue

        if tx_type == "DIVIDEND":
            row("DIVIDEND", abs(amount), Decimal("1"), asset_key=isin or None, row_fees=fees,
                error=None if amount else "Montant invalide")
            continue

        if tx_type in ("BUY", "SELL"):
            quantity = abs(shares)
            if not quantity:
                row(tx_type, quantity, Decimal("0"), asset_key=isin or None,
                    error=f"Quantité invalide: « {cell(line, 'shares')} »")
                continue
            price = (abs(amount) / quantity).quantize(_PRICE_STEP, ROUND_DOWN) if amount else abs(decimal_cell(line, "price"))
            if tx_type == "BUY":
                fees += ipo_fees.pop(isin, Decimal("0"))
            held[isin] = held.get(isin, Decimal("0")) + (quantity if tx_type == "BUY" else -quantity)
            if tx_type == "BUY":
                last_buy_at = executed_at
            row(tx_type, quantity, price, asset_key=isin or None, row_fees=fees)
            continue

        if tx_type == "SPLIT":
            # Market prices come back adjusted for the split, so only lines
            # restated on today's share count value right: done in the file.
            row("BUY" if shares > 0 else "SELL", abs(shares), Decimal("0"), asset_key=isin,
                error="Division d'actions non prise en charge : ramenez les lignes d'avant "
                      "sur le nombre d'actions actuel dans le fichier, puis retirez celle-ci")
            continue

        if tx_type == "WORTHLESS":
            quantity = abs(shares)
            if held.get(isin, Decimal("0")) >= quantity > 0:
                held[isin] -= quantity
                row("SELL", quantity, Decimal("0"), asset_key=isin, notes="Titre radié sans valeur")
            else:
                # Rights never bought here (a distribution): nothing to write off.
                ignored[tx_type] = ignored.get(tx_type, 0) + 1
            continue

        row("?", abs(shares) or abs(amount), Decimal("0"), asset_key=isin or None,
            error=f"Type d'opération inconnu: « {raw_type} »")

    warnings = [
        f"{count} ligne(s) {tx_type} ignorée(s) : {_IGNORED.get(tx_type, 'titres sans valeur jamais achetés')}"
        for tx_type, count in ignored.items()
    ]
    return rows, warnings


# ── Older community exports ──────────────────────────────────

_ALIASES = {
    "date": ("date", "datum", "zeitpunkt", "date d'exécution"),
    "type": ("type", "typ", "transaction type", "art"),
    "isin": ("isin",),
    "name": ("name", "nom", "titre", "instrument", "wertpapier"),
    "quantity": ("shares", "quantité", "quantite", "anzahl", "quantity", "stück", "stuck", "nombre"),
    "price": ("price", "prix", "preis", "cours", "price per share"),
    "fees": ("fee", "frais", "gebühr", "gebuhr", "fees", "commission"),
    "total": ("total", "montant", "betrag", "amount", "gesamt"),
}

_TYPE_ALIASES = {
    "kauf": "BUY", "sparplan": "BUY", "savings plan execution": "BUY",
    "verkauf": "SELL",
    "dividende": "DIVIDEND", "dividend": "DIVIDEND", "ausschüttung": "DIVIDEND",
    "einzahlung": "DEPOSIT", "deposit": "DEPOSIT", "dépôt": "DEPOSIT", "depot": "DEPOSIT",
    "auszahlung": "WITHDRAW", "withdrawal": "WITHDRAW", "retrait": "WITHDRAW",
}

_STOCK_TYPES = {"BUY", "SELL", "DEPOSIT", "DIVIDEND", "WITHDRAW"}


def _get(line: dict, field: str) -> str:
    for key, value in line.items():
        if key and key.strip().lower() in _ALIASES[field]:
            return (value or "").strip()
    return ""


def _parse_legacy(csv_content: str, options: dict) -> tuple[list[StockImportRowPreview], list[str]]:
    type_mapping = {**_TYPE_ALIASES, **(options.get("type_mapping") or {})}
    date_format = options.get("date_format")
    decimal_separator = options.get("decimal_separator")

    lines, warnings = read_rows(csv_content, options)
    rows: list[StockImportRowPreview] = []

    for i, line in enumerate(lines):
        raw_date = _get(line, "date")
        raw_type = _get(line, "type")
        isin = _get(line, "isin").upper()
        raw_qty = _get(line, "quantity")
        raw_price = _get(line, "price")
        raw_fees = _get(line, "fees")
        raw_total = _get(line, "total")

        executed_at = parse_generic_date(raw_date, date_format)
        tx_type = map_type(raw_type, type_mapping, _STOCK_TYPES, default=None)
        quantity = parse_generic_decimal(raw_qty, decimal_separator)
        price = parse_generic_decimal(raw_price, decimal_separator)
        fees = parse_generic_decimal(raw_fees, decimal_separator) or Decimal("0")
        total = parse_generic_decimal(raw_total, decimal_separator)

        error = None
        if executed_at is None:
            error = f"Date invalide: « {raw_date} »"
        elif tx_type is None:
            error = f"Type d'opération inconnu: « {raw_type} »"

        if tx_type in ("DEPOSIT", "WITHDRAW"):
            amount = total if total is not None else quantity
            if (amount is None or amount == 0) and error is None:
                error = "Montant invalide"
            rows.append(StockImportRowPreview(
                row_index=i,
                executed_at=executed_at.isoformat() if executed_at else raw_date,
                type=tx_type,
                asset_key="EUR",
                amount=float(abs(amount)) if amount is not None else 0.0,
                price_per_unit=1.0,
                fees=float(abs(fees)),
                error=error,
            ))
            continue

        if tx_type == "DIVIDEND":
            amount = total if total is not None else quantity
            if (amount is None or amount == 0) and error is None:
                error = "Montant invalide"
            rows.append(StockImportRowPreview(
                row_index=i,
                executed_at=executed_at.isoformat() if executed_at else raw_date,
                type="DIVIDEND",
                asset_key=isin or None,
                isin=isin or None,
                name=_get(line, "name") or None,
                amount=float(abs(amount)) if amount is not None else 0.0,
                price_per_unit=1.0,
                fees=float(abs(fees)),
                needs_asset_key=not isin,
                error=error,
            ))
            continue

        # BUY / SELL
        if (quantity is None or quantity == 0) and error is None:
            error = f"Quantité invalide: « {raw_qty} »"
        if price is None and total is not None and quantity:
            price = abs(total) / abs(quantity)

        rows.append(StockImportRowPreview(
            row_index=i,
            executed_at=executed_at.isoformat() if executed_at else raw_date,
            type=tx_type or "?",
            asset_key=isin or None,
            isin=isin or None,
            name=_get(line, "name") or None,
            amount=float(abs(quantity)) if quantity is not None else 0.0,
            price_per_unit=float(price) if price is not None else 0.0,
            fees=float(abs(fees)),
            needs_asset_key=not isin,
            error=error,
        ))

    return rows, warnings


def parse_trade_republic(csv_content: str, options: dict) -> tuple[list[StockImportRowPreview], list[str]]:
    if is_export(csv_content):
        return _parse_export(csv_content)
    return _parse_legacy(csv_content, options)


@register
class TradeRepublicParser(StockImportParser):
    """Trade Republic CSV: the app's transaction export, or a community format."""

    source_id = "trade_republic"
    label = "Trade Republic (export CSV)"
    file_hint = (
        "export « Transactions » de l'app Trade Republic — les cryptos s'importent "
        "à part, depuis la page Crypto"
    )

    def detect(self, csv_content: str) -> float:
        if is_export(csv_content):
            return 0.95
        header = csv_header_line(csv_content).lower()
        cols = {c.strip().strip('"') for c in header.replace(";", ",").split(",")}
        if "isin" not in cols:
            return 0.0
        score = 0.0
        for field in ("date", "type", "quantity", "price"):
            if cols & set(_ALIASES[field]):
                score += 0.2
        return min(score + 0.2, 0.85) if score >= 0.6 else 0.0

    def parse(self, csv_content: str, options: dict) -> tuple[list[StockImportRowPreview], list[str]]:
        return parse_trade_republic(csv_content, options)
