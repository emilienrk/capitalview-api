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
from decimal import Decimal, InvalidOperation
from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlmodel import Session

from models.enums import CryptoTransactionType
from dtos.crypto import (
    BinanceImportRowPreview,
    BinanceImportGroupPreview,
    BinanceImportPreviewResponse,
    BinanceImportConfirmResponse,
    CryptoTransactionCreate,
)
from services.crypto_transaction import create_crypto_transaction


# ── Constants ─────────────────────────────────────────────────

STABLECOIN_SYMBOLS = frozenset({"USDC", "USDT", "DAI", "BUSD", "FDUSD"})


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

        # Parse timestamp
        try:
            utc_time = datetime.fromisoformat(utc_str.replace(" ", "T"))
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
            return CryptoTransactionType.FIAT_DEPOSIT, coin, amount, Decimal("1")
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
                return CryptoTransactionType.FIAT_DEPOSIT, coin, amount, Decimal("1")
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
            return CryptoTransactionType.FIAT_DEPOSIT, coin, amount, Decimal("1")
        return CryptoTransactionType.BUY, coin, amount, Decimal("0")

    # ── Fallback (unknown operation) ──────────────────────
    if positive:
        return CryptoTransactionType.BUY, coin, amount, Decimal("0")
    return CryptoTransactionType.SPEND, coin, amount, Decimal("0")


# ── Group summary ─────────────────────────────────────────────

def _group_summary(rows: list[_BinanceRow]) -> str:
    """Human-readable one-liner for a group."""
    out_syms: list[str] = []
    in_syms: list[str] = []
    for r in rows:
        target = in_syms if r.change > 0 else out_syms
        if r.coin not in target:
            target.append(r.coin)

    if out_syms and in_syms:
        return f"{', '.join(out_syms)} → {', '.join(in_syms)}"
    if in_syms:
        return f"+ {', '.join(in_syms)}"
    if out_syms:
        return f"- {', '.join(out_syms)}"
    return ", ".join({r.operation for r in rows})


# ── Preview ───────────────────────────────────────────────────

def generate_preview(csv_content: str) -> BinanceImportPreviewResponse:
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

    # Group by proximity (within 6 seconds)
    sorted_rows = sorted(rows, key=lambda r: r.utc_time)
    buckets: list[list[_BinanceRow]] = []
    for r in sorted_rows:
        t = r.utc_time.replace(microsecond=0)
        if buckets and (t - buckets[-1][0].utc_time.replace(microsecond=0)).total_seconds() <= 6:
            buckets[-1].append(r)
        else:
            buckets.append([r])

    groups: list[BinanceImportGroupPreview] = []
    needing_eur = 0

    for idx, group_rows in enumerate(buckets):
        ts = group_rows[0].utc_time.replace(microsecond=0)

        # Map every row
        mapped: list[BinanceImportRowPreview] = []
        has_eur = False
        eur_out = Decimal("0")
        eur_in = Decimal("0")
        usdc_total = Decimal("0")
        has_trade = False

        for r in group_rows:
            tx_type, symbol, amount, price = _map_row(r)

            mapped.append(BinanceImportRowPreview(
                operation=r.operation,
                coin=r.coin,
                change=float(r.change),
                mapped_type=tx_type.value,
                mapped_symbol=symbol,
                mapped_amount=float(amount),
                mapped_price=float(price),
            ))

            if r.coin == "EUR":
                has_eur = True
                if r.change < 0:
                    eur_out += abs(r.change)
                else:
                    eur_in += abs(r.change)

            if r.coin in STABLECOIN_SYMBOLS:
                usdc_total += abs(r.change)

            if tx_type in (
                CryptoTransactionType.BUY,
                CryptoTransactionType.SPEND,
            ) and symbol != "EUR":
                has_trade = True

        # Sort mapped rows: BUY first, then SPEND, then FEE, then others
        _TYPE_ORDER = {"BUY": 0, "FIAT_DEPOSIT": 1, "REWARD": 1, "SPEND": 2, "EXIT": 3, "FEE": 4, "FIAT_ANCHOR": 5, "TRANSFER": 6}
        mapped.sort(key=lambda m: _TYPE_ORDER.get(m.mapped_type, 99))

        # Determine EUR anchor status
        is_reward_only = all(r.operation == "Crypto Box" for r in group_rows)
        is_transfer_only = all(r.operation == "Withdraw" for r in group_rows)
        needs_eur = not has_eur and not is_reward_only and not is_transfer_only and has_trade

        if needs_eur:
            needing_eur += 1

        # Auto EUR amount for groups that already contain EUR
        auto_eur = float(eur_out) if eur_out > 0 else (float(eur_in) if eur_in > 0 else None)

        groups.append(BinanceImportGroupPreview(
            group_index=idx,
            timestamp=ts.isoformat(),
            rows=mapped,
            summary=_group_summary(group_rows),
            has_eur=has_eur,
            auto_eur_amount=auto_eur if has_eur else None,
            needs_eur_input=needs_eur,
            hint_usdc_amount=float(usdc_total) if usdc_total > 0 and needs_eur else None,
            eur_amount=auto_eur if has_eur else None,
        ))

    return BinanceImportPreviewResponse(
        total_groups=len(groups),
        total_rows=sum(len(g.rows) for g in groups),
        groups_needing_eur=needing_eur,
        groups=groups,
    )


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
    an additional FIAT_ANCHOR EUR row is inserted.
    """
    total_imported = 0

    for group in groups:
        group_uuid = str(uuid4())
        timestamp = datetime.fromisoformat(group.timestamp)

        # Create one atomic row per mapped CSV line
        for row in group.rows:
            amount = Decimal(str(row.mapped_amount))
            if amount <= 0:
                continue  # safety guard

            tx = CryptoTransactionCreate(
                account_id=account_id,
                symbol=row.mapped_symbol,
                type=CryptoTransactionType(row.mapped_type),
                amount=amount,
                price_per_unit=Decimal(str(row.mapped_price)),
                executed_at=timestamp,
            )
            create_crypto_transaction(session, tx, master_key, group_uuid=group_uuid)
            total_imported += 1

        # Add FIAT_ANCHOR if group needs EUR and user provided an amount > 0
        if group.needs_eur_input and group.eur_amount is not None and group.eur_amount > 0:
            anchor = CryptoTransactionCreate(
                account_id=account_id,
                symbol="EUR",
                type=CryptoTransactionType.FIAT_ANCHOR,
                amount=Decimal(str(group.eur_amount)),
                price_per_unit=Decimal("1"),
                executed_at=timestamp,
            )
            create_crypto_transaction(session, anchor, master_key, group_uuid=group_uuid)
            total_imported += 1

    return BinanceImportConfirmResponse(
        imported_count=total_imported,
        groups_count=len(groups),
    )
