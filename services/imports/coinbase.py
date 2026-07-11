"""
Coinbase transaction-history CSV import.

Coinbase exports start with a few preamble lines before the real header
(``ID,Timestamp,Transaction Type,Asset,Quantity Transacted,…`` or the older
``Timestamp,Transaction Type,…`` with "Spot Price" columns). Each CSV line
is one composite operation, decomposed into a group of atomic rows.

Amounts in a non-EUR currency are never converted silently: the group is
flagged ``needs_eur_input`` instead.
"""

import csv
import io
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

from dtos.crypto import BinanceImportPreviewResponse
from models.enums import CryptoTransactionType
from services.imports._crypto_common import (
    CryptoImportParser,
    MappedRow,
    build_crypto_preview,
)
from services.imports.registry import register

_HEADER_TOKENS = ("timestamp", "transaction type", "asset", "quantity transacted")

_CONVERT_NOTES_RE = re.compile(
    r"Converted\s+([\d.,]+)\s+(\S+)\s+to\s+([\d.,]+)\s+(\S+)", re.IGNORECASE
)


def _dec(value: str | None) -> Decimal | None:
    """Parse a Coinbase numeric field ("€1,234.56", "$0.001", "1234.5")."""
    if not value:
        return None
    cleaned = re.sub(r"[^0-9.,\-]", "", value.strip())
    if not cleaned:
        return None
    # US format: comma = thousands separator
    cleaned = cleaned.replace(",", "")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _parse_time(value: str) -> datetime | None:
    value = value.strip().replace("Z", "").replace(" UTC", "").replace("T", " ")
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _find_header_index(lines: list[str]) -> int | None:
    for i, line in enumerate(lines):
        lowered = line.lower()
        if all(token in lowered for token in _HEADER_TOKENS):
            return i
    return None


def _get(line: dict, *names: str) -> str:
    for name in names:
        for key, value in line.items():
            if key and key.strip().lower() == name:
                return (value or "").strip()
    return ""


def _map_line(line: dict) -> tuple[datetime, list[MappedRow]] | None:
    ts = _parse_time(_get(line, "timestamp"))
    if ts is None:
        return None

    tx_type = _get(line, "transaction type").lower()
    asset = _get(line, "asset").upper()
    quantity = _dec(_get(line, "quantity transacted"))
    currency = (_get(line, "price currency", "spot price currency") or "EUR").upper()
    subtotal = _dec(_get(line, "subtotal"))
    total = _dec(_get(line, "total (inclusive of fees and/or spread)", "total"))
    fees = _dec(_get(line, "fees and/or spread", "fees")) or Decimal("0")
    notes = _get(line, "notes")

    if not asset or quantity is None or quantity == 0:
        return None
    quantity = abs(quantity)
    is_eur_currency = currency == "EUR"

    rows: list[MappedRow] = []

    def add(coin: str, change: Decimal, tx: CryptoTransactionType, amount: Decimal, price: Decimal):
        rows.append(MappedRow(
            operation=tx_type or "unknown",
            coin=coin,
            change=change,
            tx_type=tx,
            asset_key=coin,
            amount=amount,
            price=price,
        ))

    if tx_type in ("buy", "advanced trade buy", "advance trade buy"):
        add(asset, quantity, CryptoTransactionType.BUY, quantity, Decimal("0"))
        if is_eur_currency and subtotal:
            add("EUR", -subtotal, CryptoTransactionType.SPEND, subtotal, Decimal("1"))
            if fees > 0:
                add("EUR", -fees, CryptoTransactionType.FEE, fees, Decimal("1"))
    elif tx_type in ("sell", "advanced trade sell", "advance trade sell"):
        add(asset, -quantity, CryptoTransactionType.SPEND, quantity, Decimal("0"))
        if is_eur_currency and (subtotal or total):
            credited = total if total is not None else subtotal
            add("EUR", credited, CryptoTransactionType.DEPOSIT, credited, Decimal("1"))
            if fees > 0:
                add("EUR", -fees, CryptoTransactionType.FEE, fees, Decimal("1"))
    elif tx_type in ("convert",):
        add(asset, -quantity, CryptoTransactionType.SPEND, quantity, Decimal("0"))
        match = _CONVERT_NOTES_RE.search(notes or "")
        if match:
            target_amount = _dec(match.group(3))
            target_asset = match.group(4).upper()
            if target_amount and target_asset:
                add(target_asset, target_amount, CryptoTransactionType.BUY, target_amount, Decimal("0"))
        if is_eur_currency and fees > 0:
            add("EUR", -fees, CryptoTransactionType.FEE, fees, Decimal("1"))
    elif tx_type in ("receive",):
        add(asset, quantity, CryptoTransactionType.BUY, quantity, Decimal("0"))
    elif tx_type in ("send",):
        add(asset, -quantity, CryptoTransactionType.TRANSFER, quantity, Decimal("0"))
    elif tx_type in (
        "rewards income", "staking income", "learning reward",
        "coinbase earn", "inflation reward", "reward",
    ):
        add(asset, quantity, CryptoTransactionType.REWARD, quantity, Decimal("0"))
    elif tx_type in ("exchange deposit", "deposit", "fiat deposit"):
        if asset == "EUR":
            add("EUR", quantity, CryptoTransactionType.DEPOSIT, quantity, Decimal("1"))
        else:
            add(asset, quantity, CryptoTransactionType.BUY, quantity, Decimal("0"))
    elif tx_type in ("exchange withdrawal", "withdrawal", "fiat withdrawal"):
        if asset == "EUR":
            add("EUR", -quantity, CryptoTransactionType.WITHDRAW, quantity, Decimal("1"))
        else:
            add(asset, -quantity, CryptoTransactionType.TRANSFER, quantity, Decimal("0"))
    else:
        # Unknown operation: conservative fallback on the quantity sign
        raw_qty = _dec(_get(line, "quantity transacted")) or quantity
        if raw_qty >= 0:
            add(asset, quantity, CryptoTransactionType.BUY, quantity, Decimal("0"))
        else:
            add(asset, -quantity, CryptoTransactionType.SPEND, quantity, Decimal("0"))

    return (ts.replace(microsecond=0), rows) if rows else None


def generate_preview(
    csv_content: str,
    session=None,
    existing_fps: set | None = None,
) -> BinanceImportPreviewResponse:
    if csv_content.startswith("\ufeff"):
        csv_content = csv_content[1:]

    lines = csv_content.splitlines()
    header_idx = _find_header_index(lines)
    if header_idx is None:
        return BinanceImportPreviewResponse(
            total_groups=0, total_rows=0, groups_needing_eur=0, groups=[],
        )

    reader = csv.DictReader(io.StringIO("\n".join(lines[header_idx:])))
    buckets = []
    for line in reader:
        mapped = _map_line(line)
        if mapped is not None:
            buckets.append(mapped)

    buckets.sort(key=lambda b: b[0])
    return build_crypto_preview(buckets, session=session, existing_fps=existing_fps)


@register
class CoinbaseParser(CryptoImportParser):
    """Coinbase transaction-history CSV export."""

    source_id = "coinbase"
    label = "Coinbase (export historique de transactions)"
    file_hint = "export CSV « Transaction history » Coinbase"

    def detect(self, csv_content: str) -> float:
        lines = csv_content.splitlines()[:10]
        return 0.9 if _find_header_index(lines) is not None else 0.0

    def generate(self, csv_content, session=None, existing_fps=None):
        return generate_preview(csv_content, session=session, existing_fps=existing_fps)
