"""
Account history service — Lazy Snapshot generation.

On every login, a BackgroundTask calls `run_lazy_catchup` which:
  1. Finds the last snapshot date for the user.
  2. Builds the list of missing dates (last+1 .. yesterday).
  3. Reads current positions ONCE per account (frozen bag).
  4. Downloads the historical price matrix ONCE for all symbols.
  5. Loops over missing dates × accounts in RAM to generate snapshots.
  6. Bulk-upserts the rows into `account_history`.

Because this runs as a BackgroundTask, it is never blocking for the user.
The function creates its own DB session (same pattern as `update_all_prices_daily`
in services/market.py) since the request session closes before the task runs.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional
import uuid
import copy

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import Session, select

from database import get_engine
from models.account_history import AccountHistory
from models.asset import Asset, AssetValuation
from models.enums import AccountCategory, AssetType
from models.market import MarketAsset, MarketPriceHistory
from models import BankAccount, CryptoAccount, StockAccount
from services.encryption import decrypt_data, encrypt_data, hash_index
from services.settings import get_or_create_settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal data structures
# ---------------------------------------------------------------------------


@dataclass
class FrozenPosition:
    """A single asset position at the moment the snapshot is taken."""

    symbol: str
    quantity: Decimal
    total_invested: Decimal  # already in EUR


@dataclass
class IndividualAsset:
    """Représente un actif physique unique et son historique de prix."""
    name: str
    acquired_at: date
    sold_at: Optional[date]
    invested: Decimal
    valuations: list[tuple[date, Decimal]]


@dataclass
class _AccountSnapshot:
    """All data needed to generate N daily snapshots for one account."""

    account_id: str
    account_type: AccountCategory
    account_created_at: date = field(default_factory=lambda: datetime.now(timezone.utc).date())

    # Fallback / Bank mode
    frozen_positions: list[FrozenPosition] = field(default_factory=list)
    total_invested: Decimal = Decimal("0")  # EUR

    # Exact mode for Stocks and Crypto
    transactions: list = field(default_factory=list)

    physical_assets: list[IndividualAsset] = field(default_factory=list)


def _parse_iso_date(value: str) -> Optional[date]:
    """Parse an ISO-like string into a date. Return None when invalid."""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except Exception:
        return None


def _interpolate_asset_value(
    invested: Decimal,
    acquired_at: date,
    valuations: list[tuple[date, Decimal]],
    d: date,
) -> Decimal:
    """
    Return the linearly-interpolated value of an asset on day *d*.

    The anchor timeline is built from:
      (acquired_at, invested)  +  sorted valuation pairs [(date, value), ...]

    - Before the first anchor: returns *invested* (shouldn't normally happen).
    - Between two consecutive anchors: linear interpolation.
    - After the last anchor: flat at the last anchor value.
    """
    # Build the full sorted anchor list: [(date, value), ...]
    anchors: list[tuple[date, Decimal]] = [(acquired_at, invested)] + list(valuations)

    if len(anchors) == 1 or d >= anchors[-1][0]:
        # Flat at the last known value
        return anchors[-1][1]

    # Find the segment [anchors[i], anchors[i+1]) that contains d
    for i in range(len(anchors) - 1):
        start_d, start_v = anchors[i]
        end_d, end_v = anchors[i + 1]
        if start_d <= d < end_d:
            total_days = (end_d - start_d).days
            if total_days == 0:
                return end_v
            elapsed = (d - start_d).days
            return start_v + (end_v - start_v) * Decimal(elapsed) / Decimal(total_days)

    # d is before the first anchor — return invested as safe fallback
    return invested


def _parse_positions_json(positions_json: Optional[str]) -> list[FrozenPosition]:
    """Parse decrypted positions JSON into frozen positions."""
    if not positions_json:
        return []

    try:
        parsed = json.loads(positions_json)
    except Exception:
        return []

    positions: list[FrozenPosition] = []
    for item in parsed:
        symbol = item.get("symbol")
        quantity = item.get("quantity")
        invested = item.get("invested", "0")
        if not symbol or quantity is None:
            continue
        try:
            positions.append(
                FrozenPosition(
                    symbol=str(symbol),
                    quantity=Decimal(str(quantity)),
                    total_invested=Decimal(str(invested)),
                )
            )
        except Exception:
            continue
    return positions


# ---------------------------------------------------------------------------
# Phase 2 — Utility helpers
# ---------------------------------------------------------------------------


def _get_snapshot_date_bounds(session: Session, user_uuid_bidx: str) -> dict[str, tuple[date, date]]:
    """
    Return {account_id_bidx: (first_snapshot_date, last_snapshot_date)}
    for every account of this user.
    """
    rows = session.exec(
        sa.select(
            AccountHistory.account_id_bidx,
            sa.func.min(AccountHistory.snapshot_date).label("first_date"),
            sa.func.max(AccountHistory.snapshot_date).label("last_date"),
        )
        .where(AccountHistory.user_uuid_bidx == user_uuid_bidx)
        .group_by(AccountHistory.account_id_bidx)
    ).all()
    return {
        row.account_id_bidx: (row.first_date, row.last_date)
        for row in rows
    }


def _resolve_account_start_date(
    default_created_at: date,
    opened_at: Optional[date],
    txs: Optional[list],
) -> date:
    """Pick the earliest known business start date for an account."""
    candidates: list[date] = [default_created_at]

    if opened_at is not None:
        candidates.append(opened_at)

    if txs:
        try:
            first_tx_date = min(getattr(tx, "executed_at").date() for tx in txs)
            candidates.append(first_tx_date)
        except Exception:
            pass

    return min(candidates)


def _get_price_matrix(
    session: Session,
    symbols: list[str],
    from_date: date,
    to_date: date,
) -> dict[str, dict[date, Decimal]]:
    """
    Return {symbol: {date: price}} for every (symbol, date) pair in the range.

    Uses a single JOIN query. For dates where a price is missing, the matrix
    is left sparse — callers must handle gaps (use the nearest earlier price).
    """
    if not symbols:
        return {}

    rows = session.exec(
        select(MarketAsset.isin, MarketPriceHistory.price_date, MarketPriceHistory.price)
        .join(MarketAsset, MarketPriceHistory.market_asset_id == MarketAsset.id)
        .where(
            MarketAsset.isin.in_(symbols),
            MarketPriceHistory.price_date >= from_date,
            MarketPriceHistory.price_date <= to_date,
        )
    ).all()

    matrix: dict[str, dict[date, Decimal]] = {}
    for isin, price_date, price in rows:
        matrix.setdefault(isin, {})[price_date] = price

    return matrix


def _fill_price_gaps(
    matrix: dict[str, dict[date, Decimal]],
    symbols: list[str],
    missing_dates: list[date],
    session: Session,
) -> dict[str, dict[date, Decimal]]:
    """
    For each symbol and each missing date, ensure a price exists by falling back
    to the most recent known price before that date.
    Mutates and returns the matrix.
    """
    symbols_needing_fallback = [s for s in symbols if not matrix.get(s)]

    if symbols_needing_fallback:
        subq = (
            sa.select(
                MarketAsset.isin.label("isin"),
                sa.func.max(MarketPriceHistory.price_date).label("max_date"),
            )
            .join(MarketAsset, MarketPriceHistory.market_asset_id == MarketAsset.id)
            .where(
                MarketAsset.isin.in_(symbols_needing_fallback),
                MarketPriceHistory.price_date < missing_dates[0],
            )
            .group_by(MarketAsset.isin)
            .subquery()
        )

        fallback_rows = session.exec(
            sa.select(MarketAsset.isin, MarketPriceHistory.price)
            .join(MarketAsset, MarketPriceHistory.market_asset_id == MarketAsset.id)
            .join(
                subq,
                sa.and_(
                    MarketAsset.isin == subq.c.isin,
                    MarketPriceHistory.price_date == subq.c.max_date,
                ),
            )
        ).all()

        for isin, price in fallback_rows:
            matrix.setdefault(isin, {})[missing_dates[0]] = price

    for symbol in symbols:
        prices_for_symbol = matrix.get(symbol, {})
        last_price: Optional[Decimal] = None
        for d in missing_dates:
            if d in prices_for_symbol:
                last_price = prices_for_symbol[d]
            elif last_price is not None:
                prices_for_symbol[d] = last_price
        matrix[symbol] = prices_for_symbol

    return matrix


# ---------------------------------------------------------------------------
# Phase 3 — Snapshot generator
# ---------------------------------------------------------------------------


def _generate_missing_snapshots(
    user_uuid_bidx: str,
    account_id_bidx: str,
    account_snapshot: _AccountSnapshot,
    price_matrix: dict[str, dict[date, Decimal]],
    missing_dates: list[date],
    prev_value: Decimal,
    master_key: str,
) -> list[dict]:
    """
    Generate one encrypted row dict per missing date.
    Returned rows are ready for a bulk `pg_insert(AccountHistory).values(rows)`.
    """
    now = datetime.now(timezone.utc)
    rows: list[dict] = []

    # State for exact mode (replay)
    is_exact_mode = bool(account_snapshot.transactions)
    tx_idx = 0
    txs = account_snapshot.transactions

    # Dict: symbol -> {"quantity": Decimal, "invested": Decimal}
    current_positions = {}
    current_invested = account_snapshot.total_invested

    # Seed current positions from frozen state
    for p in account_snapshot.frozen_positions:
        current_positions[p.symbol] = {"quantity": p.quantity, "invested": p.total_invested}

    # Keep a EUR cash bucket available so replay is order-independent.
    if "EUR" not in current_positions:
        current_positions["EUR"] = {"quantity": Decimal("0"), "invested": Decimal("0")}

    for d in missing_dates:
        # Phase A: Replay transactions up to day `d` if exact mode
        if is_exact_mode:
            while tx_idx < len(txs) and getattr(txs[tx_idx], "executed_at").date() <= d:
                tx = txs[tx_idx]
                sym = getattr(tx, "isin", None) or getattr(tx, "symbol", None)
                tx_type = getattr(tx, "type", "")
                amount = getattr(tx, "amount", Decimal("0"))
                price_per_unit = getattr(tx, "price_per_unit", Decimal("0"))
                fees = getattr(tx, "fees", Decimal("0"))
                
                # Skip non-position types; allow FIAT_DEPOSIT only when it's EUR cash (crypto)
                if tx_type in ("FIAT_ANCHOR", "FEE", "EXIT", "TRANSFER") or not sym:
                    tx_idx += 1
                    continue
                if tx_type == "FIAT_DEPOSIT" and sym != "EUR":
                    tx_idx += 1
                    continue

                if sym not in current_positions:
                    current_positions[sym] = {"quantity": Decimal("0"), "invested": Decimal("0")}

                is_eur_deposit = tx_type in ("DEPOSIT", "FIAT_DEPOSIT") and sym == "EUR"

                if tx_type in ("BUY", "REWARD"):
                    current_positions[sym]["quantity"] += amount
                    current_positions[sym]["invested"] += (amount * price_per_unit) + fees
                    current_invested += (amount * price_per_unit) + fees

                    # Any stock/crypto acquisition consumes EUR cash.
                    if sym != "EUR":
                        cost = (amount * price_per_unit) + fees
                        current_positions["EUR"]["quantity"] -= cost

                elif tx_type == "DIVIDEND":
                    # Cash dividend: proceeds go to EUR cash, position quantity unchanged.
                    proceeds = (amount * price_per_unit) - fees
                    current_positions["EUR"]["quantity"] += proceeds

                elif is_eur_deposit:
                    # EUR cash only: track quantity, not invested capital.
                    current_positions["EUR"]["quantity"] += (amount - fees)
                elif tx_type in ("SELL", "SPEND"):
                    if current_positions[sym]["quantity"] > 0:
                        fraction = min(amount / current_positions[sym]["quantity"], Decimal("1"))
                        current_positions[sym]["quantity"] -= amount
                        if sym != "EUR":  # EUR cash is not invested capital
                            current_positions[sym]["invested"] -= current_positions[sym]["invested"] * fraction
                            current_invested -= current_invested * fraction

                    # SELL returns EUR cash.
                    if tx_type == "SELL" and sym != "EUR":
                        proceeds = (amount * price_per_unit) - fees
                        current_positions["EUR"]["quantity"] += proceeds

                tx_idx += 1

        # Phase B: calculate the day's value
        if account_snapshot.account_type == AccountCategory.BANK:
            # Bank balance doesn't fluctuate with the market — keep it frozen
            if is_exact_mode:
                qty = current_positions.get("EUR", {}).get("quantity", Decimal("0"))
                invested = current_positions.get("EUR", {}).get("invested", Decimal("0"))
            else:
                qty = account_snapshot.frozen_positions[0].quantity if account_snapshot.frozen_positions else Decimal("0")
                invested = account_snapshot.frozen_positions[0].total_invested if account_snapshot.frozen_positions else Decimal("0")

            total_value = qty

            snapshot_positions = []
            if total_value > Decimal("0"):
                snapshot_positions.append({
                    "symbol": "EUR",
                    "quantity": str(qty),
                    "value": str(round(total_value, 2)),
                    "price": "1.00",
                    "invested": str(round(invested, 2)),
                    "percentage": "100.00"
                })
            positions_json = json.dumps(snapshot_positions) if snapshot_positions else None


        elif account_snapshot.account_type == AccountCategory.ASSET:
            total_value = Decimal("0")
            current_invested = Decimal("0")
            temp_positions = []

            for asset in account_snapshot.physical_assets:
                if d < asset.acquired_at:
                    continue

                if asset.sold_at and d >= asset.sold_at:
                    continue

                # Linear interpolation between consecutive valuation anchors
                current_val = _interpolate_asset_value(
                    asset.invested, asset.acquired_at, asset.valuations, d
                )

                total_value += current_val
                current_invested += asset.invested

                temp_positions.append({
                    "symbol": asset.name,
                    "quantity": Decimal("1"),
                    "value": current_val,
                    "price": current_val,
                    "invested": asset.invested,
                })

            snapshot_positions = []
            for p in temp_positions:
                percentage = (p["value"] / total_value) * Decimal("100") if total_value > Decimal("0") else Decimal("0")
                snapshot_positions.append({
                    "symbol": p["symbol"],
                    "quantity": str(p["quantity"]),
                    "value": str(round(p["value"], 2)),
                    "price": str(round(p["price"], 2)),
                    "invested": str(round(p["invested"], 2)),
                    "percentage": str(round(percentage, 2))
                })

            positions_json = json.dumps(snapshot_positions) if snapshot_positions else None
        else:
            total_value = Decimal("0")
            snapshot_positions = []
            temp_positions = []
            
            for sym, pos_data in current_positions.items():
                qty = pos_data["quantity"]
                # Clamp EUR quantity to 0 (can go negative from BUY deductions)
                if sym == "EUR":
                    qty = max(qty, Decimal("0"))
                if qty <= Decimal("0"):
                    continue

                # EUR cash: price is always 1, no market lookup needed
                price = Decimal("1") if sym == "EUR" else price_matrix.get(sym, {}).get(d)
                if price is not None:
                    value = qty * price
                    total_value += value
                else:
                    value = Decimal("0")
                temp_positions.append(
                    {
                        "symbol": sym,
                        "quantity": qty,
                        "value": value,
                        "price": price,
                        "invested": pos_data["invested"],
                    }
                )
            for p in temp_positions:
                if total_value > Decimal("0"):
                    percentage = (p["value"] / total_value) * Decimal("100")
                else:
                    percentage = Decimal("0")

                snapshot_positions.append(
                    {
                        "symbol": p["symbol"],
                        "quantity": str(p["quantity"]),
                        "value": str(round(p["value"], 2)),
                        "price": str(round(p["price"], 2)) if p["price"] is not None else None,
                        "invested": str(round(p["invested"], 2)),
                        "percentage": str(round(percentage, 2))
                    }
                )
            positions_json = json.dumps(snapshot_positions) if snapshot_positions else None

        daily_pnl = total_value - prev_value

        row: dict = {
            "uuid": uuid.uuid7(),
            "user_uuid_bidx": user_uuid_bidx,
            "account_id_bidx": account_id_bidx,
            "account_type": account_snapshot.account_type.value,
            "snapshot_date": d,
            "total_value_enc": encrypt_data(str(round(total_value, 2)), master_key),
            "total_invested_enc": encrypt_data(str(round(current_invested, 2)), master_key),
            "daily_pnl_enc": encrypt_data(str(round(daily_pnl, 2)), master_key),
            "positions_enc": encrypt_data(positions_json, master_key) if positions_json else None,
            "created_at": now,
            "updated_at": now,
        }
        rows.append(row)
        prev_value = total_value

    return rows


# ---------------------------------------------------------------------------
# Phase 2b — Account position extractors
# ---------------------------------------------------------------------------


def _build_stock_snapshots(
    session: Session,
    master_key: str,
    user_uuid_bidx: str,
) -> list[_AccountSnapshot]:
    """Return one _AccountSnapshot per stock account for the user."""
    from services.stock_transaction import get_account_transactions

    accounts = session.exec(
        select(StockAccount).where(StockAccount.user_uuid_bidx == user_uuid_bidx)
    ).all()

    result: list[_AccountSnapshot] = []
    for acc in accounts:
        # Recupere directement les transactions pour le mode exact.
        txs = get_account_transactions(session, acc.uuid, master_key)
        txs.sort(key=lambda x: x.executed_at)
        account_start_date = _resolve_account_start_date(
            default_created_at=acc.created_at.date(),
            opened_at=acc.opened_at,
            txs=txs,
        )
        
        result.append(
            _AccountSnapshot(
                account_id=acc.uuid,
                account_type=AccountCategory.STOCK,
                account_created_at=account_start_date,
                transactions=txs
            )
        )
    return result


def _build_crypto_snapshots(
    session: Session,
    master_key: str,
    user_uuid_bidx: str,
    show_negative: bool,
) -> list[_AccountSnapshot]:
    """Return one _AccountSnapshot per crypto account for the user."""
    from services.crypto_transaction import get_account_transactions

    accounts = session.exec(
        select(CryptoAccount).where(CryptoAccount.user_uuid_bidx == user_uuid_bidx)
    ).all()

    result: list[_AccountSnapshot] = []
    for acc in accounts:
        # Recupere directement les transactions pour le mode exact.
        txs = get_account_transactions(session, acc.uuid, master_key)
        txs.sort(key=lambda x: x.executed_at)
        account_start_date = _resolve_account_start_date(
            default_created_at=acc.created_at.date(),
            opened_at=acc.opened_at,
            txs=txs,
        )
        
        result.append(
            _AccountSnapshot(
                account_id=acc.uuid,
                account_type=AccountCategory.CRYPTO,
                account_created_at=account_start_date,
                transactions=txs
            )
        )
    return result


def _build_bank_snapshots(
    session: Session,
    master_key: str,
    user_uuid_bidx: str,
) -> list[_AccountSnapshot]:
    """Return one _AccountSnapshot per bank account (balance as frozen position)."""
    accounts = session.exec(
        select(BankAccount).where(BankAccount.user_uuid_bidx == user_uuid_bidx)
    ).all()

    result: list[_AccountSnapshot] = []
    for acc in accounts:
        balance = Decimal(decrypt_data(acc.balance_enc, master_key))
        result.append(
            _AccountSnapshot(
                account_id=acc.uuid,
                account_type=AccountCategory.BANK,
                frozen_positions=[FrozenPosition(symbol="EUR", quantity=balance, total_invested=balance)],
                total_invested=balance,
                account_created_at=acc.created_at.date(),
            )
        )
    return result


def _build_asset_snapshots(
    session: Session,
    master_key: str,
    user_uuid_bidx: str,
) -> list[_AccountSnapshot]:
    """Return one virtual _AccountSnapshot keeping physical assets detailed."""
    assets = session.exec(
        select(Asset).where(Asset.user_uuid_bidx == user_uuid_bidx)
    ).all()

    if not assets:
        return []

    asset_ids = [a.uuid for a in assets]
    valuations = session.exec(
        select(AssetValuation).where(AssetValuation.asset_uuid.in_(asset_ids))
    ).all()

    valuations_by_asset: dict[str, list[tuple[date, Decimal]]] = {}
    for valuation in valuations:
        valued_at_raw = decrypt_data(valuation.valued_at_enc, master_key)
        valued_at = _parse_iso_date(valued_at_raw)
        if valued_at is None:
            continue
        try:
            value = Decimal(decrypt_data(valuation.estimated_value_enc, master_key))
            valuations_by_asset.setdefault(valuation.asset_uuid, []).append((valued_at, value))
        except Exception:
            continue

    account_created_at = min(a.created_at.date() for a in assets)
    physical_assets: list[IndividualAsset] = []

    for asset in assets:
        try:
            name = decrypt_data(asset.name_enc, master_key)
        except Exception:
            name = "Actif inconnu"

        try:
            invested = Decimal(decrypt_data(asset.purchase_price_enc, master_key))
        except Exception:
            invested = Decimal("0")

        acquired_at = asset.created_at.date()
        if asset.acquisition_date_enc:
            acq_raw = decrypt_data(asset.acquisition_date_enc, master_key)
            parsed_acq = _parse_iso_date(acq_raw)
            if parsed_acq:
                acquired_at = parsed_acq

        sold_at: Optional[date] = None
        if asset.sold_at_enc:
            sold_at_raw = decrypt_data(asset.sold_at_enc, master_key)
            sold_at = _parse_iso_date(sold_at_raw)

        series = sorted(valuations_by_asset.get(asset.uuid, []), key=lambda item: item[0])
        
        physical_assets.append(IndividualAsset(
            name=name,
            acquired_at=acquired_at,
            sold_at=sold_at,
            invested=invested,
            valuations=series
        ))

    virtual_account_id = f"ASSET_PORTFOLIO::{user_uuid_bidx}"
    return [
        _AccountSnapshot(
            account_id=virtual_account_id,
            account_type=AccountCategory.ASSET,
            account_created_at=account_created_at,
            physical_assets=physical_assets,
            total_invested=Decimal("0"),
        )
    ]


# ---------------------------------------------------------------------------
# Phase 4 — Entry point (called as BackgroundTask from /login)
# ---------------------------------------------------------------------------


def run_lazy_catchup(user_uuid: str, master_key: str) -> None:
    """
    Compute and insert all missing daily snapshots for the user.

    Creates its own DB session (the request session is already closed when
    BackgroundTask runs). Safe to call multiple times — the upsert is idempotent.
    """
    engine = get_engine()

    with Session(engine) as session:
        user_uuid_bidx = hash_index(user_uuid, master_key)
        yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)

        # ── 1. Load settings ──────────────────────────────────────────────────
        settings = get_or_create_settings(session, user_uuid, master_key)

        # ── 2. Collect all account snapshots (frozen positions) ───────────────
        all_accounts: list[_AccountSnapshot] = []

        try:
            all_accounts += _build_stock_snapshots(session, master_key, user_uuid_bidx)
        except Exception as exc:
            logger.warning("account_history: stock snapshot error: %s", exc)

        try:
            show_negative = getattr(settings, "crypto_show_negative_positions", False)
            all_accounts += _build_crypto_snapshots(
                session, master_key, user_uuid_bidx, show_negative
            )
        except Exception as exc:
            logger.warning("account_history: crypto snapshot error: %s", exc)

        try:
            all_accounts += _build_bank_snapshots(session, master_key, user_uuid_bidx)
        except Exception as exc:
            logger.warning("account_history: bank snapshot error: %s", exc)

        try:
            all_accounts += _build_asset_snapshots(session, master_key, user_uuid_bidx)
        except Exception as exc:
            logger.warning("account_history: asset snapshot error: %s", exc)

        if not all_accounts:
            return

        # ── 3. Fetch per-account snapshot bounds ──────────────────────────────
        snapshot_date_bounds = _get_snapshot_date_bounds(session, user_uuid_bidx)

        # ── 4. Compute per-account missing dates ──────────────────────────────
        accounts_to_process: list[tuple[_AccountSnapshot, list[date]]] = []

        for acc_snap in all_accounts:
            account_id_bidx = hash_index(acc_snap.account_id, master_key)
            first_and_last = snapshot_date_bounds.get(account_id_bidx)

            # Auto-heal stale history starts from old logic (created_at-based).
            # If the true account start is earlier than existing snapshots, we
            # invalidate and rebuild the full account history once.
            if first_and_last is not None:
                first_date, _ = first_and_last
                if first_date > acc_snap.account_created_at:
                    session.exec(
                        sa.delete(AccountHistory).where(
                            AccountHistory.account_id_bidx == account_id_bidx,
                        )
                    )
                    first_and_last = None
                    logger.info(
                        "account_history catchup: invalidated stale start for account_bidx=%s (first=%s, expected_start=%s)",
                        account_id_bidx[:8] + "…",
                        first_date,
                        acc_snap.account_created_at,
                    )

            last_date = first_and_last[1] if first_and_last is not None else None

            if last_date is not None and last_date >= yesterday:
                continue  # already up-to-date

            if last_date is None:
                start_date = acc_snap.account_created_at
            else:
                start_date = last_date + timedelta(days=1)

            if start_date > yesterday:
                continue

            missing_dates = [
                start_date + timedelta(days=i)
                for i in range((yesterday - start_date).days + 1)
            ]
            accounts_to_process.append((acc_snap, missing_dates))

        if not accounts_to_process:
            return

        logger.info(
            "account_history catchup: %d account(s) need backfill for user_bidx=%s",
            len(accounts_to_process),
            user_uuid_bidx[:8] + "…",
        )

        # ── 4b. Ensure historical market prices exist for all affected symbols ──
        # Delegated to services/market.py which is the single owner of price data.
        from services.market import ensure_price_history

        earliest_date = min(d for _, dates in accounts_to_process for d in dates)

        # Build once: symbol -> set(asset_type), then reuse for both steps below.
        symbol_types_map: dict[str, set[AssetType]] = {}
        for acc_snap, _ in accounts_to_process:
            if acc_snap.account_type not in (AccountCategory.STOCK, AccountCategory.CRYPTO):
                continue

            atype = (
                AssetType.STOCK
                if acc_snap.account_type == AccountCategory.STOCK
                else AssetType.CRYPTO
            )

            symbols: set[str] = set()
            if acc_snap.transactions:
                for tx in acc_snap.transactions:
                    sym = getattr(tx, "isin", None) or getattr(tx, "symbol", None)
                    if sym and getattr(tx, "type", "") not in ("FIAT_DEPOSIT", "FIAT_ANCHOR") and sym != "EUR":
                        symbols.add(sym)

            # Include bootstrap-held symbols (important when no new txs).
            for pos in acc_snap.frozen_positions:
                if pos.symbol != "EUR":  # EUR price is always 1, no market fetch needed
                    symbols.add(pos.symbol)

            for symbol in symbols:
                symbol_types_map.setdefault(symbol, set()).add(atype)

        for symbol, asset_types in symbol_types_map.items():
            for atype in asset_types:
                ensure_price_history(session, symbol, atype, earliest_date)

        # ── 5. Build price matrix (union of all per-account date ranges) ───────
        all_symbols = list(symbol_types_map.keys())

        # Collect the full date span needed across all accounts
        all_missing_dates_sorted = sorted({
            d
            for _, dates in accounts_to_process
            for d in dates
        })

        price_matrix: dict[str, dict[date, Decimal]] = {}
        if all_symbols and all_missing_dates_sorted:
            price_matrix = _get_price_matrix(
                session, all_symbols,
                all_missing_dates_sorted[0],
                all_missing_dates_sorted[-1],
            )
            price_matrix = _fill_price_gaps(price_matrix, all_symbols, all_missing_dates_sorted, session)

        # ── 6. Generate rows for every account × its own missing dates ─────────
        all_rows: list[dict] = []

        for acc_snap, missing_dates in accounts_to_process:
            account_id_bidx = hash_index(acc_snap.account_id, master_key)

            # Bootstrap: find the last known total_value for this account
            last_row = session.exec(
                select(AccountHistory)
                .where(AccountHistory.account_id_bidx == account_id_bidx)
                .order_by(AccountHistory.snapshot_date.desc())
            ).first()

            if last_row:
                try:
                    prev_value = Decimal(decrypt_data(last_row.total_value_enc, master_key))
                except Exception:
                    prev_value = Decimal("0")
            else:
                prev_value = Decimal("0")

            # For exact-mode accounts, bootstrap from the previous snapshot to
            # avoid replaying the full transaction history on each login.
            effective_snapshot = acc_snap
            if (
                last_row
                and acc_snap.account_type in (AccountCategory.STOCK, AccountCategory.CRYPTO)
            ):
                bootstrap_positions: list[FrozenPosition] = []
                if last_row.positions_enc:
                    try:
                        positions_raw = decrypt_data(last_row.positions_enc, master_key)
                        bootstrap_positions = _parse_positions_json(positions_raw)
                    except Exception:
                        bootstrap_positions = []

                bootstrap_invested = acc_snap.total_invested
                try:
                    bootstrap_invested = Decimal(decrypt_data(last_row.total_invested_enc, master_key))
                except Exception:
                    pass

                start_date = missing_dates[0]
                filtered_txs = [
                    tx
                    for tx in acc_snap.transactions
                    if getattr(tx, "executed_at").date() >= start_date
                ]

                effective_snapshot = copy.copy(acc_snap)
                effective_snapshot.frozen_positions = bootstrap_positions
                effective_snapshot.total_invested = bootstrap_invested
                effective_snapshot.transactions = filtered_txs

            rows = _generate_missing_snapshots(
                user_uuid_bidx=user_uuid_bidx,
                account_id_bidx=account_id_bidx,
                account_snapshot=effective_snapshot,
                price_matrix=price_matrix,
                missing_dates=missing_dates,
                prev_value=prev_value,
                master_key=master_key,
            )
            all_rows.extend(rows)

        if not all_rows:
            return

        # ── 7. Bulk upsert ────────────────────────────────────────────────────
        stmt = pg_insert(AccountHistory).values(all_rows)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_account_history_account_date",
            set_={
                "total_value_enc": stmt.excluded.total_value_enc,
                "total_invested_enc": stmt.excluded.total_invested_enc,
                "daily_pnl_enc": stmt.excluded.daily_pnl_enc,
                "positions_enc": stmt.excluded.positions_enc,
                "updated_at": stmt.excluded.updated_at,
            },
        )
        try:
            session.exec(stmt)
            session.commit()
            logger.info(
                "account_history catchup: inserted/updated %d snapshots for user_bidx=%s",
                len(all_rows),
                user_uuid_bidx[:8] + "…",
            )
        except Exception as exc:
            session.rollback()
            logger.error("account_history catchup failed during bulk upsert: %s", exc)
            raise


# ---------------------------------------------------------------------------
# Phase 5 — Retroactive rebuild (called when a past-dated transaction changes)
# ---------------------------------------------------------------------------


def rebuild_account_history_from_date(
    user_uuid: str,
    account_id_bidx: str,
    from_date: date,
    master_key: str,
    symbols: Optional[list[str]] = None,
    asset_type: Optional[AssetType] = None,
) -> None:
    """
    Delete all AccountHistory rows for *account_id_bidx* from *from_date* onwards,
    backfill missing market price history for the affected symbols, then re-run
    a full lazy catchup so the cleared range is rebuilt with real daily prices.

    Designed to be called as a FastAPI BackgroundTask whenever a transaction is
    created, updated, or deleted with an *executed_at* date that falls before today.
    """
    from services.market import ensure_price_history

    engine = get_engine()

    with Session(engine) as session:
        deleted = session.exec(
            sa.delete(AccountHistory).where(
                AccountHistory.account_id_bidx == account_id_bidx,
                AccountHistory.snapshot_date >= from_date,
            )
        )
        session.commit()
        logger.info(
            "account_history: invalidated %d snapshot(s) from %s for account_bidx=%s",
            deleted.rowcount,
            from_date,
            account_id_bidx[:8] + "…",
        )

        if symbols and asset_type is not None:
            for symbol in symbols:
                ensure_price_history(session, symbol, asset_type, from_date)

    run_lazy_catchup(user_uuid, master_key)


def trigger_post_transaction_updates(
    session: Session,
    background_tasks: "BackgroundTasks", # type: ignore
    user_uuid: str,
    master_key: str,
    asset_type: AssetType,
    affected_dates: list[Optional[date]],
    affected_assets: list[Optional[str]],
    account_id: Optional[str] = None,
    account_id_bidx: Optional[str] = None,
) -> None:
    """
    Centralize post-transaction business orchestration:
    - Update community positions
    - Trigger retroactive account history rebuild if past dates are affected
    """
    from services.community import refresh_community_positions

    # 1. Update community positions
    refresh_community_positions(session, user_uuid, master_key)

    # 2. Filter affected dates that are strictly in the past
    yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)
    past_dates = [d for d in affected_dates if d is not None and d <= yesterday]

    if past_dates:
        earliest_date = min(past_dates)
        if not account_id_bidx and account_id:
            account_id_bidx = hash_index(account_id, master_key)
            
        if not account_id_bidx:
            return

        # Clean the list of assets — exclude EUR (price=1, no market data needed)
        clean_assets = list({a for a in affected_assets if a and a != "EUR"})

        background_tasks.add_task(
            rebuild_account_history_from_date,
            user_uuid,
            account_id_bidx,
            earliest_date,
            master_key,
            clean_assets,
            asset_type
        )

