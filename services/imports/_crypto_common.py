"""
Shared crypto-import machinery (grouping, preview analysis, EUR prefill,
execution). Used by every crypto parser (Binance, Kraken, Coinbase, generic).

A parser only has to produce time-ordered buckets of :class:`MappedRow`
(atomic ledger rows); everything downstream is common.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from uuid import uuid4

from sqlmodel import Session, select

from dtos.crypto import (
    BinanceImportGroupPreview,
    BinanceImportPreviewResponse,
    BinanceImportRowPreview,
    CryptoTransactionCreate,
)
from models.enums import AssetType, CryptoTransactionType
from models.market import MarketAsset, MarketPriceHistory
from services.crypto_transaction import create_crypto_transaction
from services.imports.dedup import Fingerprint, make_fingerprint

logger = logging.getLogger(__name__)

STABLECOIN_SYMBOLS = frozenset({"USDC", "USDT", "DAI", "BUSD", "FDUSD"})

_TYPE_ORDER = {
    "BUY": 0, "DEPOSIT": 1, "REWARD": 1, "SPEND": 2,
    "WITHDRAW": 3, "FEE": 4, "ANCHOR": 5, "TRANSFER": 6,
}


@dataclass
class MappedRow:
    """One atomic ledger row produced by a parser."""
    operation: str          # original platform label (for display)
    coin: str               # original asset symbol (for display)
    change: Decimal         # signed original amount
    tx_type: CryptoTransactionType
    asset_key: str
    amount: Decimal
    price: Decimal


def group_summary(rows: list[MappedRow]) -> str:
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


def build_crypto_preview(
    buckets: list[tuple[datetime, list[MappedRow]]],
    session: Session | None = None,
    existing_fps: set[Fingerprint] | None = None,
) -> BinanceImportPreviewResponse:
    """
    Turn parser buckets into the standard grouped crypto preview:
    EUR anchor detection, stablecoin hints, duplicate flags, and
    historical-price prefill of missing EUR amounts.
    """
    groups: list[BinanceImportGroupPreview] = []
    needing_eur = 0

    for idx, (ts, group_rows) in enumerate(buckets):
        ts = ts.replace(microsecond=0)

        mapped: list[BinanceImportRowPreview] = []
        has_eur = False
        eur_out = Decimal("0")
        eur_in = Decimal("0")
        usdc_total = Decimal("0")
        has_trade = False
        all_duplicates = bool(group_rows) and existing_fps is not None

        for r in group_rows:
            mapped.append(BinanceImportRowPreview(
                operation=r.operation,
                coin=r.coin,
                change=float(r.change),
                mapped_type=r.tx_type.value,
                mapped_asset_key=r.asset_key,
                mapped_amount=float(r.amount),
                mapped_price=float(r.price),
            ))

            if existing_fps is not None:
                fp = make_fingerprint(ts, r.asset_key, r.tx_type.value, r.amount)
                if fp not in existing_fps:
                    all_duplicates = False

            if r.coin == "EUR":
                has_eur = True
                if r.change < 0:
                    eur_out += abs(r.change)
                else:
                    eur_in += abs(r.change)

            if r.coin in STABLECOIN_SYMBOLS:
                usdc_total += abs(r.change)

            if r.tx_type in (
                CryptoTransactionType.BUY,
                CryptoTransactionType.SPEND,
            ) and r.asset_key != "EUR":
                has_trade = True

        # Sort mapped rows: BUY first, then SPEND, then FEE, then others
        mapped.sort(key=lambda m: _TYPE_ORDER.get(m.mapped_type, 99))

        # Determine EUR anchor status
        is_reward_only = all(r.tx_type == CryptoTransactionType.REWARD for r in group_rows)
        is_transfer_only = all(r.tx_type == CryptoTransactionType.TRANSFER for r in group_rows)
        needs_eur = not has_eur and not is_reward_only and not is_transfer_only and has_trade

        if needs_eur:
            needing_eur += 1

        # Auto EUR amount for groups that already contain EUR
        auto_eur = float(eur_out) if eur_out > 0 else (float(eur_in) if eur_in > 0 else None)

        groups.append(BinanceImportGroupPreview(
            group_index=idx,
            timestamp=ts.isoformat(),
            rows=mapped,
            summary=group_summary(group_rows),
            has_eur=has_eur,
            auto_eur_amount=auto_eur if has_eur else None,
            needs_eur_input=needs_eur,
            hint_usdc_amount=float(usdc_total) if usdc_total > 0 and needs_eur else None,
            eur_amount=auto_eur if has_eur else None,
            is_duplicate=all_duplicates,
        ))

    if session is not None:
        try:
            prefill_eur_amounts(session, groups)
        except Exception as exc:
            logger.warning("build_crypto_preview: prefill_eur_amounts failed: %s", exc)

    return BinanceImportPreviewResponse(
        total_groups=len(groups),
        total_rows=sum(len(g.rows) for g in groups),
        groups_needing_eur=needing_eur,
        groups=groups,
    )


def prefill_eur_amounts(session: Session, groups: list[BinanceImportGroupPreview]) -> None:
    """
    For groups that need manual EUR input and have no eur_amount yet,
    look up the historical CRYPTO→EUR price at the transaction date and
    pre-fill eur_amount = total_buy_quantity × price.
    """
    from services.market import ensure_price_history

    # Collect groups to fill and their main buy symbol
    to_fill: list[tuple[BinanceImportGroupPreview, str, float, date]] = []

    for group in groups:
        if not group.needs_eur_input or group.eur_amount is not None:
            continue

        buy_rows = [r for r in group.rows if r.mapped_type == "BUY"]
        if not buy_rows:
            continue

        # Pick primary symbol: prefer non-stablecoin, non-EUR
        primary_row = next(
            (r for r in buy_rows if r.mapped_asset_key not in STABLECOIN_SYMBOLS and r.mapped_asset_key != "EUR"),
            buy_rows[0],
        )
        symbol = primary_row.mapped_asset_key
        total_amount = sum(r.mapped_amount for r in buy_rows if r.mapped_asset_key == symbol)

        tx_date = datetime.fromisoformat(group.timestamp).date()
        to_fill.append((group, symbol, total_amount, tx_date))

    if not to_fill:
        return

    # Compute min date per symbol across all groups
    symbol_min_dates: dict[str, date] = {}
    for _, symbol, _, tx_date in to_fill:
        if symbol not in symbol_min_dates or tx_date < symbol_min_dates[symbol]:
            symbol_min_dates[symbol] = tx_date

    all_symbols = list(symbol_min_dates.keys())

    # Query DB first — prices may already be there from a previous import
    def _fetch_matrix(symbols: list[str]) -> dict[str, dict[date, Decimal]]:
        if not symbols:
            return {}
        db_rows = session.exec(
            select(MarketAsset.asset_key, MarketPriceHistory.price_date, MarketPriceHistory.price)
            .join(MarketAsset, MarketPriceHistory.market_asset_id == MarketAsset.id)
            .where(MarketAsset.asset_key.in_(symbols))
        ).all()
        result: dict[str, dict[date, Decimal]] = {}
        for asset_key, price_date, price in db_rows:
            result.setdefault(asset_key, {})[price_date] = price
        return result

    matrix = _fetch_matrix(all_symbols)

    # Only call ensure_price_history for symbols that have no price data in DB yet
    symbols_needing_backfill = [
        symbol for symbol in all_symbols if symbol not in matrix
    ]
    for symbol in symbols_needing_backfill:
        min_date = symbol_min_dates[symbol]
        try:
            ensure_price_history(session, symbol, AssetType.CRYPTO, min_date)
        except Exception as exc:
            logger.debug("prefill_eur_amounts: ensure_price_history failed for %s: %s", symbol, exc)

    # Re-fetch only for the symbols that just got backfilled
    if symbols_needing_backfill:
        new_entries = _fetch_matrix(symbols_needing_backfill)
        matrix.update(new_entries)

    # Fill eur_amount for each group needing it
    for group, symbol, total_amount, tx_date in to_fill:
        price_map = matrix.get(symbol, {})
        if not price_map:
            continue

        # Closest price on or before tx_date; fall back to earliest known
        candidates = [(d, p) for d, p in price_map.items() if d <= tx_date]
        if candidates:
            _, best_price = max(candidates, key=lambda x: x[0])
        else:
            _, best_price = min(price_map.items(), key=lambda x: x[0])

        group.eur_amount = round(float(Decimal(str(total_amount)) * best_price), 2)


def execute_crypto_groups(
    session: Session,
    account_id: str,
    groups: list[BinanceImportGroupPreview],
    master_key: str,
    skip_duplicates: bool = False,
    existing_fps: set[Fingerprint] | None = None,
) -> tuple[int, int]:
    """
    Create all atomic transaction rows from confirmed import groups.

    A group is skipped when ``skip_duplicates`` is set and every one of its
    rows already exists on the account (server-side fingerprints — client
    flags are never trusted). Groups flagged ``needs_eur_input`` with a
    positive ``eur_amount`` get an extra ANCHOR EUR row.

    Returns:
        (imported_count, skipped_duplicate_groups)
    """
    total_imported = 0
    skipped = 0

    for group in groups:
        timestamp = datetime.fromisoformat(group.timestamp)

        if skip_duplicates and existing_fps and group.rows:
            fps = [
                make_fingerprint(timestamp, row.mapped_asset_key, row.mapped_type, row.mapped_amount)
                for row in group.rows
            ]
            if all(fp in existing_fps for fp in fps):
                skipped += 1
                continue

        group_uuid = str(uuid4())

        # Create one atomic row per mapped CSV line
        for row in group.rows:
            amount = Decimal(str(row.mapped_amount))
            if amount <= 0:
                continue  # safety guard

            tx = CryptoTransactionCreate(
                account_id=account_id,
                asset_key=row.mapped_asset_key,
                type=CryptoTransactionType(row.mapped_type),
                amount=amount,
                price_per_unit=Decimal(str(row.mapped_price)),
                executed_at=timestamp,
            )
            create_crypto_transaction(session, tx, master_key, group_uuid=group_uuid)
            total_imported += 1

        # Add ANCHOR if group needs EUR and user provided an amount > 0
        if group.needs_eur_input and group.eur_amount is not None and group.eur_amount > 0:
            anchor = CryptoTransactionCreate(
                account_id=account_id,
                asset_key="EUR",
                type=CryptoTransactionType.ANCHOR,
                amount=Decimal(str(group.eur_amount)),
                price_per_unit=Decimal("1"),
                executed_at=timestamp,
            )
            create_crypto_transaction(session, anchor, master_key, group_uuid=group_uuid)
            total_imported += 1

    return total_imported, skipped
