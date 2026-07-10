"""
Generic CSV import with user-provided column mapping.

The user (frontend) supplies ``options`` describing how to read the file:

  {
    "mapping": {            # CSV column names → target fields
      "date": "Date",
      "type": "Type",              # optional (default per parser)
      "asset": "Symbole",          # asset symbol / ticker
      "quantity": "Quantité",
      "price": "Prix unitaire",    # optional
      "fees": "Frais",             # optional
    },
    "delimiter": ";",              # optional (auto-sniffed)
    "decimal_separator": ",",      # optional (auto: both handled)
    "date_format": "%d/%m/%Y",     # optional (auto: ISO, FR, US)
    "type_mapping": {"Achat": "BUY", "Vente": "SELL"},  # optional
  }

Rows that fail to parse are reported with an ``error`` field in the preview
instead of failing the whole import; confirm refuses rows still in error.
"""

import csv
import io
from datetime import datetime
from decimal import Decimal, InvalidOperation

from sqlmodel import Session

from dtos.imports import (
    ImportConfirmRequest,
    ImportConfirmResponse,
    ImportPreviewResponse,
    StockImportRowPreview,
)
from models.enums import CryptoTransactionType, StockTransactionType
from services.imports._crypto_common import (
    CryptoImportParser,
    MappedRow,
    build_crypto_preview,
)
from services.imports.base import ImportCategory, ImportParser
from services.imports.dedup import make_fingerprint, stock_fingerprints
from services.imports.registry import register

_DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
    "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y",
    "%d-%m-%Y %H:%M:%S", "%d-%m-%Y",
    "%m/%d/%Y %H:%M:%S", "%m/%d/%Y",
    "%d.%m.%Y",
)


def parse_generic_date(value: str, date_format: str | None = None) -> datetime | None:
    value = value.strip()
    if not value:
        return None
    if date_format:
        try:
            return datetime.strptime(value, date_format)
        except ValueError:
            return None
    try:
        return datetime.fromisoformat(value.replace("Z", ""))
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def parse_generic_decimal(value: str, decimal_separator: str | None = None) -> Decimal | None:
    cleaned = value.strip().replace(" ", "").replace(" ", "")
    cleaned = "".join(c for c in cleaned if c in "0123456789.,-+")
    if not cleaned:
        return None
    if decimal_separator == ",":
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif decimal_separator == ".":
        cleaned = cleaned.replace(",", "")
    else:
        # Auto: if both present, the rightmost is the decimal separator
        if "," in cleaned and "." in cleaned:
            if cleaned.rfind(",") > cleaned.rfind("."):
                cleaned = cleaned.replace(".", "").replace(",", ".")
            else:
                cleaned = cleaned.replace(",", "")
        elif "," in cleaned:
            cleaned = cleaned.replace(",", ".")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def read_rows(csv_content: str, options: dict) -> tuple[list[dict], list[str]]:
    """Read the CSV into dict rows. Returns (rows, warnings)."""
    if csv_content.startswith("\ufeff"):
        csv_content = csv_content[1:]

    warnings: list[str] = []
    delimiter = options.get("delimiter")
    if not delimiter:
        sample = "\n".join(csv_content.splitlines()[:5])
        try:
            delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t").delimiter
        except csv.Error:
            delimiter = ","
            warnings.append("Délimiteur non détecté, virgule utilisée par défaut")

    reader = csv.DictReader(io.StringIO(csv_content), delimiter=delimiter)
    return list(reader), warnings


def get_mapped(line: dict, mapping: dict, field: str) -> str:
    column = mapping.get(field)
    if not column:
        return ""
    for key, value in line.items():
        if key and key.strip().lower() == column.strip().lower():
            return (value or "").strip()
    return ""


def map_type(raw: str, type_mapping: dict, allowed: set[str], default: str | None = None) -> str | None:
    """Resolve a raw CSV type label to an allowed transaction type."""
    if not raw:
        return default
    # Explicit user mapping first (case-insensitive)
    for k, v in (type_mapping or {}).items():
        if k.strip().lower() == raw.strip().lower():
            return v.upper() if v.upper() in allowed else None
    upper = raw.strip().upper()
    if upper in allowed:
        return upper
    # Common FR/EN aliases
    aliases = {
        "ACHAT": "BUY", "VENTE": "SELL", "DÉPÔT": "DEPOSIT", "DEPOT": "DEPOSIT",
        "RETRAIT": "WITHDRAW", "DIVIDENDE": "DIVIDEND", "FRAIS": "FEE",
        "RÉCOMPENSE": "REWARD", "RECOMPENSE": "REWARD", "STAKING": "REWARD",
        "TRANSFERT": "TRANSFER", "WITHDRAWAL": "WITHDRAW",
    }
    mapped = aliases.get(upper)
    if mapped and mapped in allowed:
        return mapped
    return None


# ── Generic STOCK parser ─────────────────────────────────────

_STOCK_TYPES = {t.value for t in StockTransactionType}


def parse_stock_rows(csv_content: str, options: dict) -> tuple[list[StockImportRowPreview], list[str]]:
    mapping = options.get("mapping") or {}
    type_mapping = options.get("type_mapping") or {}
    date_format = options.get("date_format")
    decimal_separator = options.get("decimal_separator")

    lines, warnings = read_rows(csv_content, options)
    rows: list[StockImportRowPreview] = []

    for i, line in enumerate(lines):
        raw_date = get_mapped(line, mapping, "date")
        raw_type = get_mapped(line, mapping, "type")
        raw_asset = get_mapped(line, mapping, "asset")
        raw_qty = get_mapped(line, mapping, "quantity")
        raw_price = get_mapped(line, mapping, "price")
        raw_fees = get_mapped(line, mapping, "fees")

        error = None
        executed_at = parse_generic_date(raw_date, date_format)
        if executed_at is None:
            error = f"Date invalide: « {raw_date} »"

        tx_type = map_type(raw_type, type_mapping, _STOCK_TYPES, default="BUY")
        if tx_type is None and error is None:
            error = f"Type d'opération inconnu: « {raw_type} »"

        quantity = parse_generic_decimal(raw_qty, decimal_separator)
        if (quantity is None or quantity == 0) and error is None:
            error = f"Quantité invalide: « {raw_qty} »"

        price = parse_generic_decimal(raw_price, decimal_separator) if raw_price else Decimal("0")
        fees = parse_generic_decimal(raw_fees, decimal_separator) if raw_fees else Decimal("0")

        asset_key = raw_asset.upper() or None
        if tx_type == "DEPOSIT":
            asset_key = "EUR"
            price = Decimal("1")

        rows.append(StockImportRowPreview(
            row_index=i,
            executed_at=executed_at.isoformat() if executed_at else raw_date,
            type=tx_type or (raw_type or "?"),
            asset_key=asset_key,
            amount=float(abs(quantity)) if quantity is not None else 0.0,
            price_per_unit=float(price or 0),
            fees=float(fees or 0),
            needs_asset_key=(asset_key is None and tx_type not in ("DEPOSIT", "WITHDRAW")),
            error=error,
        ))

    return rows, warnings


class _StockItem:
    """Adapter: preview row → object accepted by bulk_create_stock_transactions."""

    def __init__(self, row: StockImportRowPreview):
        self.asset_key = row.asset_key
        self.type = StockTransactionType(row.type)
        self.amount = Decimal(str(row.amount))
        self.price_per_unit = Decimal(str(row.price_per_unit))
        self.fees = Decimal(str(row.fees))
        self.executed_at = datetime.fromisoformat(row.executed_at)
        self.notes = row.notes


def flag_stock_duplicates(rows: list[StockImportRowPreview], existing_fps: set) -> int:
    count = 0
    for row in rows:
        if row.error:
            continue
        fp = make_fingerprint(row.executed_at, row.asset_key or "", row.type, row.amount)
        if fp in existing_fps:
            row.is_duplicate = True
            count += 1
    return count


def execute_stock_rows(
    session: Session,
    account_id: str,
    rows: list[StockImportRowPreview],
    master_key: str,
    skip_duplicates: bool,
) -> ImportConfirmResponse:
    """Shared execute for every stock parser (generic, Degiro, Trade Republic)."""
    from fastapi import HTTPException, status

    from services.stock_transaction import bulk_create_stock_transactions

    errored = [r for r in rows if r.error]
    if errored:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{len(errored)} ligne(s) en erreur — corrigez ou retirez-les avant de confirmer",
        )
    missing_asset = [r for r in rows if r.needs_asset_key or (not r.asset_key and r.type in ("BUY", "SELL", "DIVIDEND"))]
    if missing_asset:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{len(missing_asset)} ligne(s) sans actif identifié (ISIN/symbole) — complétez-les avant de confirmer",
        )

    skip_fps = None
    if skip_duplicates:
        skip_fps = stock_fingerprints(session, account_id, master_key)

    created, skipped = bulk_create_stock_transactions(
        session, account_id, [_StockItem(r) for r in rows], master_key,
        skip_fingerprints=skip_fps,
    )
    return ImportConfirmResponse(
        imported_count=len(created),
        skipped_duplicates=skipped,
    )


@register
class GenericStockParser(ImportParser):
    """Any broker CSV, mapped column by column by the user."""

    source_id = "generic_stock"
    label = "CSV générique (actions) avec mapping de colonnes"
    category = ImportCategory.STOCK
    file_hint = "n'importe quel CSV de courtier (mapping manuel des colonnes)"
    supports_mapping = True

    def detect(self, csv_content: str) -> float:
        return 0.0  # never auto-detected

    def preview(
        self,
        session: Session,
        csv_content: str,
        options: dict,
        *,
        account_id: str | None = None,
        master_key: str | None = None,
    ) -> ImportPreviewResponse:
        rows, warnings = parse_stock_rows(csv_content, options)

        duplicates = 0
        if account_id and master_key:
            duplicates = flag_stock_duplicates(
                rows, stock_fingerprints(session, account_id, master_key)
            )

        return ImportPreviewResponse(
            source_id=self.source_id,
            category=self.category.value,
            total_rows=len(rows),
            duplicates_count=duplicates,
            error_rows=sum(1 for r in rows if r.error),
            warnings=warnings,
            stock_rows=rows,
        )

    def execute(
        self,
        session: Session,
        account_id: str,
        payload: ImportConfirmRequest,
        master_key: str,
    ) -> ImportConfirmResponse:
        return execute_stock_rows(
            session, account_id, payload.stock_rows or [], master_key, payload.skip_duplicates
        )


# ── Generic CRYPTO parser ────────────────────────────────────

_CRYPTO_TYPES = {t.value for t in CryptoTransactionType}


@register
class GenericCryptoParser(CryptoImportParser):
    """Any exchange/wallet CSV, mapped column by column by the user."""

    source_id = "generic_crypto"
    label = "CSV générique (crypto) avec mapping de colonnes"
    file_hint = "n'importe quel CSV d'exchange ou de wallet (mapping manuel des colonnes)"
    supports_mapping = True

    def detect(self, csv_content: str) -> float:
        return 0.0  # never auto-detected

    def generate(self, csv_content, session=None, existing_fps=None, options: dict | None = None):
        options = options or {}
        mapping = options.get("mapping") or {}
        type_mapping = options.get("type_mapping") or {}
        date_format = options.get("date_format")
        decimal_separator = options.get("decimal_separator")

        lines, _warnings = read_rows(csv_content, options)
        buckets: list[tuple[datetime, list[MappedRow]]] = []

        for line in lines:
            executed_at = parse_generic_date(get_mapped(line, mapping, "date"), date_format)
            if executed_at is None:
                continue
            raw_type = get_mapped(line, mapping, "type")
            tx_type_str = map_type(raw_type, type_mapping, _CRYPTO_TYPES, default="BUY")
            if tx_type_str is None:
                continue
            asset = get_mapped(line, mapping, "asset").upper()
            quantity = parse_generic_decimal(get_mapped(line, mapping, "quantity"), decimal_separator)
            if not asset or quantity is None or quantity == 0:
                continue
            price = parse_generic_decimal(get_mapped(line, mapping, "price"), decimal_separator)

            tx_type = CryptoTransactionType(tx_type_str)
            is_eur = asset == "EUR"
            if is_eur:
                price = Decimal("1")

            rows = [MappedRow(
                operation=raw_type or tx_type_str,
                coin=asset,
                change=abs(quantity) if tx_type in CryptoTransactionType.credit_types() else -abs(quantity),
                tx_type=tx_type,
                asset_key=asset,
                amount=abs(quantity),
                price=price if price is not None else Decimal("0"),
            )]
            # A known unit price provides the EUR counterpart leg
            if price and price > 0 and not is_eur and tx_type in (
                CryptoTransactionType.BUY, CryptoTransactionType.SPEND,
            ):
                eur_total = abs(quantity) * price
                if tx_type == CryptoTransactionType.BUY:
                    rows.append(MappedRow(
                        operation=raw_type or tx_type_str, coin="EUR", change=-eur_total,
                        tx_type=CryptoTransactionType.SPEND, asset_key="EUR",
                        amount=eur_total, price=Decimal("1"),
                    ))
                else:
                    rows.append(MappedRow(
                        operation=raw_type or tx_type_str, coin="EUR", change=eur_total,
                        tx_type=CryptoTransactionType.DEPOSIT, asset_key="EUR",
                        amount=eur_total, price=Decimal("1"),
                    ))

            buckets.append((executed_at.replace(microsecond=0), rows))

        buckets.sort(key=lambda b: b[0])
        return build_crypto_preview(buckets, session=session, existing_fps=existing_fps)

    def preview(
        self,
        session: Session,
        csv_content: str,
        options: dict,
        *,
        account_id: str | None = None,
        master_key: str | None = None,
    ) -> ImportPreviewResponse:
        from services.imports.dedup import crypto_fingerprints

        existing_fps = None
        if account_id and master_key:
            existing_fps = crypto_fingerprints(session, account_id, master_key)

        crypto = self.generate(csv_content, session=session, existing_fps=existing_fps, options=options)
        return ImportPreviewResponse(
            source_id=self.source_id,
            category=self.category.value,
            total_rows=crypto.total_rows,
            duplicates_count=sum(1 for g in crypto.groups if g.is_duplicate),
            crypto=crypto,
        )
