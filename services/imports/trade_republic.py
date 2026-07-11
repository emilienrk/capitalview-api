"""
Trade Republic CSV import.

Trade Republic has no official stable CSV export; this parser targets the
common community export format (FR/EN/DE column aliases):

  Date/Datum, Type/Typ, ISIN, Name/Nom, Shares/Quantité/Anzahl,
  Price/Prix/Preis, Fee/Frais/Gebühr, Total/Montant/Betrag

asset_key = ISIN. If the file does not match, ``generic_stock`` (manual
column mapping) is the fallback.
"""

from decimal import Decimal

from dtos.imports import StockImportRowPreview
from services.imports.base import csv_header_line
from services.imports.generic_csv import (
    StockImportParser,
    map_type,
    parse_generic_date,
    parse_generic_decimal,
    read_rows,
)
from services.imports.registry import register

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


def parse_trade_republic(csv_content: str, options: dict) -> tuple[list[StockImportRowPreview], list[str]]:
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


@register
class TradeRepublicParser(StockImportParser):
    """Trade Republic CSV export (community formats, FR/EN/DE)."""

    source_id = "trade_republic"
    label = "Trade Republic (export CSV)"
    file_hint = "export CSV Trade Republic — sinon utilisez le CSV générique (mapping manuel)"

    def detect(self, csv_content: str) -> float:
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
