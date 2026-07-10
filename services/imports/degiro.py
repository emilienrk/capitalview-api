"""
Degiro CSV import — handles both export files (FR and EN headers):

- ``Transactions.csv``: BUY/SELL rows (signed quantity, ISIN, price, fees).
- ``Account.csv``: cash movements; only deposits, withdrawals and dividends
  are imported — trade rows are ignored (they live in Transactions.csv).

Degiro uses unnamed currency columns next to amount columns, so parsing is
positional, driven by the named header cells. asset_key = ISIN (the app
resolves market data from it).
"""

import csv
import io
from datetime import datetime
from decimal import Decimal

from dtos.imports import StockImportRowPreview
from services.imports.generic_csv import (
    StockImportParser,
    parse_generic_decimal,
)
from services.imports.registry import register


def _parse_date(date_str: str, time_str: str = "") -> datetime | None:
    date_str = date_str.strip()
    time_str = time_str.strip()
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            d = datetime.strptime(date_str, fmt)
            if time_str:
                try:
                    t = datetime.strptime(time_str, "%H:%M")
                    d = d.replace(hour=t.hour, minute=t.minute)
                except ValueError:
                    pass
            return d
        except ValueError:
            continue
    return None


def _header_index(header: list[str], *names: str) -> int | None:
    lowered = [h.strip().lower() for h in header]
    for name in names:
        for i, h in enumerate(lowered):
            if h == name or (name and h.startswith(name)):
                return i
    return None


def _cell(row: list[str], idx: int | None) -> str:
    if idx is None or idx >= len(row):
        return ""
    return (row[idx] or "").strip()


def _read(csv_content: str) -> tuple[list[str], list[list[str]]]:
    if csv_content.startswith("\ufeff"):
        csv_content = csv_content[1:]
    reader = csv.reader(io.StringIO(csv_content))
    rows = [r for r in reader if any(c.strip() for c in r)]
    if not rows:
        return [], []
    return rows[0], rows[1:]


def _is_transactions_file(header: list[str]) -> bool:
    return (
        _header_index(header, "code isin", "isin") is not None
        and _header_index(header, "quantité", "quantity") is not None
        and _header_index(header, "cours", "price") is not None
    )


def _is_account_file(header: list[str]) -> bool:
    return (
        _header_index(header, "description") is not None
        and _header_index(header, "mouvements", "change") is not None
    )


_DEPOSIT_LABELS = ("dépôt", "depot", "versement", "deposit", "ideal")
_WITHDRAW_LABELS = ("retrait", "withdrawal", "processed flatex withdrawal")
_DIVIDEND_LABELS = ("dividende", "dividend")


def parse_degiro(csv_content: str) -> tuple[list[StockImportRowPreview], list[str]]:
    header, lines = _read(csv_content)
    if not header:
        return [], []

    rows: list[StockImportRowPreview] = []
    warnings: list[str] = []

    if _is_transactions_file(header):
        i_date = _header_index(header, "date")
        i_time = _header_index(header, "heure", "time")
        i_name = _header_index(header, "produit", "product")
        i_isin = _header_index(header, "code isin", "isin")
        i_qty = _header_index(header, "quantité", "quantity")
        i_price = _header_index(header, "cours", "price")
        i_fees = _header_index(header, "frais de courtage", "frais", "transaction costs", "transaction and/or third")

        for idx, line in enumerate(lines):
            executed_at = _parse_date(_cell(line, i_date), _cell(line, i_time))
            isin = _cell(line, i_isin).upper()
            qty = parse_generic_decimal(_cell(line, i_qty))
            price = parse_generic_decimal(_cell(line, i_price))
            fees = parse_generic_decimal(_cell(line, i_fees)) or Decimal("0")

            error = None
            if executed_at is None:
                error = f"Date invalide: « {_cell(line, i_date)} »"
            elif qty is None or qty == 0:
                error = f"Quantité invalide: « {_cell(line, i_qty)} »"

            rows.append(StockImportRowPreview(
                row_index=idx,
                executed_at=executed_at.isoformat() if executed_at else _cell(line, i_date),
                type="SELL" if (qty is not None and qty < 0) else "BUY",
                asset_key=isin or None,
                isin=isin or None,
                name=_cell(line, i_name) or None,
                amount=float(abs(qty)) if qty is not None else 0.0,
                price_per_unit=float(price) if price is not None else 0.0,
                fees=float(abs(fees)),
                needs_asset_key=not isin,
                error=error,
            ))
        return rows, warnings

    if _is_account_file(header):
        i_date = _header_index(header, "date")
        i_time = _header_index(header, "heure", "time")
        i_name = _header_index(header, "produit", "product")
        i_isin = _header_index(header, "code isin", "isin")
        i_desc = _header_index(header, "description")
        i_mvt = _header_index(header, "mouvements", "change")

        skipped_other = 0
        row_index = 0
        for line in lines:
            desc = _cell(line, i_desc).lower()
            # Amount sits in the unnamed column right after the currency column
            amount = parse_generic_decimal(_cell(line, i_mvt + 1)) if i_mvt is not None else None
            if amount is None:
                amount = parse_generic_decimal(_cell(line, i_mvt))

            if any(label in desc for label in _DIVIDEND_LABELS) and "impôt" not in desc and "tax" not in desc:
                tx_type = "DIVIDEND"
            elif any(label in desc for label in _DEPOSIT_LABELS):
                tx_type = "DEPOSIT"
            elif any(label in desc for label in _WITHDRAW_LABELS):
                tx_type = "WITHDRAW"
            else:
                skipped_other += 1
                continue

            executed_at = _parse_date(_cell(line, i_date), _cell(line, i_time))
            isin = _cell(line, i_isin).upper()

            error = None
            if executed_at is None:
                error = f"Date invalide: « {_cell(line, i_date)} »"
            elif amount is None or amount == 0:
                error = "Montant invalide"

            rows.append(StockImportRowPreview(
                row_index=row_index,
                executed_at=executed_at.isoformat() if executed_at else _cell(line, i_date),
                type=tx_type,
                asset_key="EUR" if tx_type in ("DEPOSIT", "WITHDRAW") else (isin or None),
                isin=isin or None,
                name=_cell(line, i_name) or None,
                amount=float(abs(amount)) if amount is not None else 0.0,
                price_per_unit=1.0,
                fees=0.0,
                needs_asset_key=(tx_type == "DIVIDEND" and not isin),
                error=error,
            ))
            row_index += 1

        if skipped_other:
            warnings.append(
                f"{skipped_other} ligne(s) ignorée(s) (achats/ventes et frais divers : "
                "importez Transactions.csv pour les ordres)"
            )
        return rows, warnings

    return [], ["Format Degiro non reconnu (attendu : Transactions.csv ou Account.csv)"]


@register
class DegiroParser(StockImportParser):
    """Degiro Transactions.csv / Account.csv exports."""

    source_id = "degiro"
    label = "Degiro (Transactions.csv ou Account.csv)"
    file_hint = "exports CSV Degiro (FR ou EN)"

    def detect(self, csv_content: str) -> float:
        header, _ = _read(csv_content)
        if not header:
            return 0.0
        if _is_transactions_file(header):
            # Degiro-specific: unnamed currency columns + ISIN + order id
            has_order = _header_index(header, "id ordre", "order id") is not None
            return 0.9 if has_order else 0.6
        if _is_account_file(header):
            return 0.8 if _header_index(header, "code isin", "isin") is not None else 0.4
        return 0.0

    def parse(self, csv_content: str, options: dict) -> tuple[list[StockImportRowPreview], list[str]]:
        return parse_degiro(csv_content)
