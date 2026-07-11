"""
Binance CSV import — parser, mapper, and executor.

Supports the standard Binance export format:
  User_ID, UTC_Time, Account, Operation, Coin, Change, Remark

Grouping rule: rows within a 6-second window belong to one
group and receive a shared ``group_uuid``.

Supported operations:
  Deposit, Withdraw, Buy Crypto With Fiat, Crypto Box,
  Binance Convert, Transaction Buy, Transaction Spend,
  Transaction Fee, Transaction Sold, Transaction Revenue
"""

import csv
import io
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

from sqlmodel import Session

from dtos.crypto import (
    BinanceImportConfirmResponse,
    BinanceImportGroupPreview,
    BinanceImportPreviewResponse,
)
from models.enums import CryptoTransactionType
from services.imports._crypto_common import (
    STABLECOIN_SYMBOLS,  # noqa: F401 — re-exported for backwards compatibility
    CryptoImportParser,
    MappedRow,
    build_crypto_preview,
    execute_crypto_groups,
)
from services.imports.base import csv_header_line
from services.imports.registry import register

# ── Internal dataclass ────────────────────────────────────────

@dataclass
class _BinanceRow:
    """One parsed CSV line."""
    utc_time: datetime
    account: str
    operation: str
    coin: str
    change: Decimal
    remark: str


# ── CSV Parsing ───────────────────────────────────────────────

def _parse_csv(content: str) -> list[_BinanceRow]:
    """Read raw CSV text and return structured rows."""
    # Strip BOM if present
    if content.startswith("\ufeff"):
        content = content[1:]

    reader = csv.DictReader(io.StringIO(content))
    rows: list[_BinanceRow] = []

    for line in reader:
        utc_str = (
            line.get("UTC_Time")
            or line.get("UTC Time")
            or line.get("utc_time")
            or ""
        ).strip()
        operation = (line.get("Operation") or line.get("operation") or "").strip()
        coin = (line.get("Coin") or line.get("coin") or "").upper().strip()
        change_str = (line.get("Change") or line.get("change") or "0").strip()
        account = (line.get("Account") or line.get("account") or "").strip()
        remark = (line.get("Remark") or line.get("remark") or "").strip()

        # Parse timestamp — handles both YYYY-MM-DD and YY-MM-DD (Binance 2-digit year export)
        try:
            normalized = utc_str.replace(" ", "T")
            # 2-digit year: "26-01-01T..." → "2026-01-01T..."
            parts = normalized.split("-", 1)
            if len(parts[0]) == 2:
                normalized = "20" + normalized
            utc_time = datetime.fromisoformat(normalized)
        except (ValueError, AttributeError):
            continue

        # Parse change (handles scientific notation like 6.8E-7)
        try:
            change = Decimal(change_str)
        except (InvalidOperation, AttributeError):
            continue

        if change == 0:
            continue  # Skip zero-change rows

        rows.append(_BinanceRow(
            utc_time=utc_time,
            account=account,
            operation=operation,
            coin=coin,
            change=change,
            remark=remark,
        ))

    return rows


# ── Row → Atomic type mapping ─────────────────────────────────

def _map_row(row: _BinanceRow) -> tuple[CryptoTransactionType, str, Decimal, Decimal]:
    """
    Map one CSV row to an atomic ledger row.

    Returns ``(type, symbol, amount, price_per_unit)``.
    """
    coin = row.coin
    is_eur = coin == "EUR"
    amount = abs(row.change)
    positive = row.change > 0
    op = row.operation

    # ── Deposit ───────────────────────────────────────────
    if op == "Deposit":
        if is_eur:
            return CryptoTransactionType.DEPOSIT, coin, amount, Decimal("1")
        return CryptoTransactionType.BUY, coin, amount, Decimal("0")

    # ── Withdraw ──────────────────────────────────────────
    if op == "Withdraw":
        return CryptoTransactionType.TRANSFER, coin, amount, Decimal("0")

    # ── Buy Crypto With Fiat ──────────────────────────────
    if op == "Buy Crypto With Fiat":
        if is_eur:
            return CryptoTransactionType.SPEND, coin, amount, Decimal("1")
        return CryptoTransactionType.BUY, coin, amount, Decimal("0")

    # ── Crypto Box (reward) ───────────────────────────────
    if op == "Crypto Box":
        return CryptoTransactionType.REWARD, coin, amount, Decimal("0")

    # ── Binance Convert ───────────────────────────────────
    if op == "Binance Convert":
        if positive:
            if is_eur:
                return CryptoTransactionType.DEPOSIT, coin, amount, Decimal("1")
            return CryptoTransactionType.BUY, coin, amount, Decimal("0")
        else:
            if is_eur:
                return CryptoTransactionType.SPEND, coin, amount, Decimal("1")
            return CryptoTransactionType.SPEND, coin, amount, Decimal("0")

    # ── Transaction Buy ───────────────────────────────────
    if op == "Transaction Buy":
        return CryptoTransactionType.BUY, coin, amount, Decimal("0")

    # ── Transaction Spend ─────────────────────────────────
    if op == "Transaction Spend":
        price = Decimal("1") if is_eur else Decimal("0")
        return CryptoTransactionType.SPEND, coin, amount, price

    # ── Transaction Fee ───────────────────────────────────
    if op == "Transaction Fee":
        return CryptoTransactionType.FEE, coin, amount, Decimal("0")

    # ── Transaction Sold ──────────────────────────────────
    if op == "Transaction Sold":
        price = Decimal("1") if is_eur else Decimal("0")
        return CryptoTransactionType.SPEND, coin, amount, price

    # ── Transaction Revenue ───────────────────────────────
    if op == "Transaction Revenue":
        if is_eur:
            return CryptoTransactionType.DEPOSIT, coin, amount, Decimal("1")
        return CryptoTransactionType.BUY, coin, amount, Decimal("0")

    # ── Fallback (unknown operation) ──────────────────────
    if positive:
        return CryptoTransactionType.BUY, coin, amount, Decimal("0")
    return CryptoTransactionType.SPEND, coin, amount, Decimal("0")


# ── Preview ───────────────────────────────────────────────────────────────────

def _build_buckets(rows: list[_BinanceRow]) -> list[tuple[datetime, list[MappedRow]]]:
    """Group rows by proximity (within 6 seconds) and map them."""
    sorted_rows = sorted(rows, key=lambda r: r.utc_time)
    buckets: list[list[_BinanceRow]] = []
    for r in sorted_rows:
        t = r.utc_time.replace(microsecond=0)
        if buckets and (t - buckets[-1][0].utc_time.replace(microsecond=0)).total_seconds() <= 6:
            buckets[-1].append(r)
        else:
            buckets.append([r])

    result: list[tuple[datetime, list[MappedRow]]] = []
    for group_rows in buckets:
        mapped: list[MappedRow] = []
        for r in group_rows:
            tx_type, symbol, amount, price = _map_row(r)
            mapped.append(MappedRow(
                operation=r.operation,
                coin=r.coin,
                change=r.change,
                tx_type=tx_type,
                asset_key=symbol,
                amount=amount,
                price=price,
            ))
        result.append((group_rows[0].utc_time.replace(microsecond=0), mapped))
    return result


def generate_preview(
    csv_content: str,
    session: Session | None = None,
    existing_fps: set | None = None,
) -> BinanceImportPreviewResponse:
    """
    Parse a Binance CSV and return a preview of all groups
    with their mapped atomic rows, indicating which groups
    need a manual EUR anchor from the user.
    """
    rows = _parse_csv(csv_content)

    if not rows:
        return BinanceImportPreviewResponse(
            total_groups=0,
            total_rows=0,
            groups_needing_eur=0,
            groups=[],
        )

    return build_crypto_preview(_build_buckets(rows), session=session, existing_fps=existing_fps)


# ── Confirm & execute ─────────────────────────────────────────

def execute_import(
    session: Session,
    account_id: str,
    groups: list[BinanceImportGroupPreview],
    master_key: str,
) -> BinanceImportConfirmResponse:
    """
    Create all atomic transaction rows from the confirmed import groups.

    For groups that ``needs_eur_input`` and have a non-zero ``eur_amount``,
    an additional ANCHOR EUR row is inserted.
    """
    total_imported, _ = execute_crypto_groups(session, account_id, groups, master_key)
    return BinanceImportConfirmResponse(
        imported_count=total_imported,
        groups_count=len(groups),
    )


# ── Registry parser ───────────────────────────────────────────

@register
class BinanceParser(CryptoImportParser):
    """Binance transaction-history CSV export."""

    source_id = "binance"
    label = "Binance (export historique de transactions)"
    file_hint = "export CSV « Transaction History » Binance"

    _HEADERS = {"user_id", "utc_time", "account", "operation", "coin", "change"}

    def detect(self, csv_content: str) -> float:
        header = csv_header_line(csv_content).lower().replace(" ", "_")
        cols = {c.strip().strip('"') for c in header.split(",")}
        hits = len(self._HEADERS & cols)
        return hits / len(self._HEADERS) if hits >= 4 else 0.0

    def generate(self, csv_content, session=None, existing_fps=None):
        return generate_preview(csv_content, session=session, existing_fps=existing_fps)
