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
import uuid

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
from models.enums import CryptoTransactionType, StockTransactionType

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal data structures
# ---------------------------------------------------------------------------

_ZERO = Decimal("0")
_CASH_IN_TYPES = frozenset({
    StockTransactionType.DEPOSIT,
    CryptoTransactionType.DEPOSIT,
})

_CASH_OUT_TYPES = frozenset({
    # StockTransactionType.WITHDRAWAL,
    CryptoTransactionType.WITHDRAW,
    CryptoTransactionType.TRANSFER,
})

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
    sold_at: date | None
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
    total_invested: Decimal = _ZERO  # EUR

    # Exact mode for Stocks and Crypto
    transactions: list = field(default_factory=list)
    show_negative_positions: bool = False

    physical_assets: list[IndividualAsset] = field(default_factory=list)


def _parse_iso_date(value: str) -> date | None:
    """Parse an ISO-like string into a date. Return None when invalid."""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except Exception:
        return None


def _to_decimal(value: object) -> Decimal:
    """Best-effort Decimal conversion; returns 0 on failure."""
    if value is None:
        return _ZERO
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:
        return _ZERO


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
    opened_at: date | None,
    txs: list | None = None,
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
    Ensure every symbol has a price for every date in missing_dates via forward-fill.
    For symbols with no price at or before missing_dates[0], a SQL fallback fetches
    the most recent known price before the range to seed the forward-fill.
    """
    if not missing_dates:
        return matrix

    first_date = missing_dates[0]

    # Seed needed: no data at all, OR earliest entry is after first_date
    symbols_needing_seed = [
        s for s in symbols
        if not matrix.get(s) or min(matrix[s].keys()) > first_date
    ]

    if symbols_needing_seed:
        subq = (
            sa.select(
                MarketAsset.isin.label("isin"),
                sa.func.max(MarketPriceHistory.price_date).label("max_date"),
            )
            .join(MarketAsset, MarketPriceHistory.market_asset_id == MarketAsset.id)
            .where(
                MarketAsset.isin.in_(symbols_needing_seed),
                MarketPriceHistory.price_date < first_date,
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
            matrix.setdefault(isin, {})[first_date] = price

    # Forward-fill across all missing_dates for every symbol
    for symbol in symbols:
        prices_for_symbol = matrix.setdefault(symbol, {})
        last_price: Decimal | None = None
        for d in missing_dates:
            if d in prices_for_symbol:
                last_price = prices_for_symbol[d]
            elif last_price is not None:
                prices_for_symbol[d] = last_price

    return matrix

def _compute_daily_net_flow(
    account_snapshot: _AccountSnapshot,
    d: date,
) -> Decimal:
    """Signed external cash flow for day d.

    Internal reallocations (e.g. STOCK BUY/SELL inside the account) are ignored.
    """
    if account_snapshot.account_type not in (AccountCategory.STOCK, AccountCategory.CRYPTO):
        return _ZERO

    net_flow = _ZERO

    day_transactions: list = []
    for tx in account_snapshot.transactions or []:
        executed_at = getattr(tx, "executed_at", None)
        if executed_at is None:
            continue
        if executed_at.date() == d:
            day_transactions.append(tx)

    for tx in day_transactions:
        tx_type = getattr(tx, "type", None)
        if tx_type in _CASH_IN_TYPES:
            net_flow += getattr(tx, "total_cost", _ZERO) or _ZERO
        elif tx_type in _CASH_OUT_TYPES:
            net_flow -= getattr(tx, "total_cost", _ZERO) or _ZERO
    return net_flow

def _build_positions_from_summary(
    summary,
) -> tuple[Decimal, Decimal, str | None]:
    """Convert AccountSummaryResponse into snapshot payload fields."""
    computed_total = _ZERO
    temp_positions: list[dict] = []

    for pos in summary.positions:
        qty = Decimal(pos.total_amount)
        if qty == _ZERO:
            continue

        price = Decimal(pos.current_price) if pos.current_price is not None else None
        invested = Decimal(pos.total_invested)
        if pos.current_value is not None:
            value = Decimal(pos.current_value)
        elif price is not None:
            value = qty * price
        else:
            value = _ZERO

        computed_total += value
        temp_positions.append(
            {
                "symbol": pos.symbol,
                "quantity": qty,
                "value": value,
                "price": price,
                "invested": invested,
            }
        )

    total_value = Decimal(summary.current_value) if summary.current_value is not None else computed_total
    current_invested = Decimal(summary.total_invested)

    snapshot_positions = []
    for p in temp_positions:
        if total_value > _ZERO:
            percentage = (p["value"] / total_value) * Decimal("100")
        else:
            percentage = _ZERO

        snapshot_positions.append(
            {
                "symbol": p["symbol"],
                "quantity": str(p["quantity"]),
                "value": str(round(p["value"], 2)),
                "price": str(round(p["price"], 2)) if p["price"] is not None else None,
                "invested": str(round(p["invested"], 2)),
                "percentage": str(round(percentage, 2)),
            }
        )

    positions_json = json.dumps(snapshot_positions) if snapshot_positions else None
    return total_value, current_invested, positions_json


# ---------------------------------------------------------------------------
# Phase 3 — Snapshot generator
# ---------------------------------------------------------------------------


def _generate_missing_snapshots(
    session: Session,
    user_uuid_bidx: str,
    account_id_bidx: str,
    account_snapshot: _AccountSnapshot,
    price_matrix: dict[str, dict[date, Decimal]],
    missing_dates: list[date],
    prev_value: Decimal,
    master_key: str,
    has_previous_snapshot: bool = True,
) -> list[dict]:
    """
    Generate one encrypted row dict per missing date.
    Returned rows are ready for a bulk `pg_insert(AccountHistory).values(rows)`.
    """
    now = datetime.now(timezone.utc)
    rows: list[dict] = []

    from services.crypto_transaction import get_crypto_account_summary
    from services.stock_transaction import get_stock_account_summary

    for day_index, d in enumerate(missing_dates):
        current_invested = account_snapshot.total_invested

        if account_snapshot.account_type == AccountCategory.BANK:
            # Bank balance doesn't fluctuate with the market — keep it frozen
            qty = account_snapshot.frozen_positions[0].quantity if account_snapshot.frozen_positions else _ZERO
            invested = account_snapshot.frozen_positions[0].total_invested if account_snapshot.frozen_positions else _ZERO

            total_value = qty

            snapshot_positions = []
            if total_value > _ZERO:
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
            total_value = _ZERO
            current_invested = _ZERO
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
                percentage = (p["value"] / total_value) * Decimal("100") if total_value > _ZERO else _ZERO
                snapshot_positions.append({
                    "symbol": p["symbol"],
                    "quantity": str(p["quantity"]),
                    "value": str(round(p["value"], 2)),
                    "price": str(round(p["price"], 2)),
                    "invested": str(round(p["invested"], 2)),
                    "percentage": str(round(percentage, 2))
                })

            positions_json = json.dumps(snapshot_positions) if snapshot_positions else None
        elif account_snapshot.account_type == AccountCategory.STOCK:
            preloaded_prices: dict[str, Decimal] = {}
            for tx in account_snapshot.transactions:
                isin = getattr(tx, "isin", None)
                if isin and isin != "EUR":
                    price = price_matrix.get(isin, {}).get(d)
                    if price is not None:
                        preloaded_prices[isin] = price

            summary = get_stock_account_summary(
                session=session,
                transactions=list(account_snapshot.transactions),
                as_of=d,
                db_only=True,
                preloaded_prices=preloaded_prices,
            )
            total_value, current_invested, positions_json = _build_positions_from_summary(summary)
        else:  # AccountCategory.CRYPTO
            preloaded_prices = {}
            for tx in account_snapshot.transactions:
                symbol = getattr(tx, "symbol", None)
                if symbol and symbol != "EUR":
                    price = price_matrix.get(symbol, {}).get(d)
                    if price is not None:
                        preloaded_prices[symbol] = price

            summary = get_crypto_account_summary(
                session=session,
                transactions=list(account_snapshot.transactions),
                show_negative_positions=account_snapshot.show_negative_positions,
                as_of=d,
                db_only=True,
                preloaded_prices=preloaded_prices,
            )
            total_value, current_invested, positions_json = _build_positions_from_summary(summary)

        if day_index == 0 and not has_previous_snapshot:
            # First snapshot has no prior day reference; avoid artificial spike vs zero.
            daily_pnl = _ZERO
        else:
            net_flow = _compute_daily_net_flow(account_snapshot, d)
            daily_pnl = total_value - prev_value - net_flow

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
                transactions=txs,
                show_negative_positions=show_negative,
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
                account_created_at=_resolve_account_start_date(
                    default_created_at=acc.created_at.date(),
                    opened_at=getattr(acc, "opened_at", None),
                    txs=None,
                ),
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
        try:
            valued_at_raw = decrypt_data(valuation.valued_at_enc, master_key)
        except Exception:
            valued_at_raw = valuation.valued_at_enc
        valued_at = _parse_iso_date(valued_at_raw)
        if valued_at is None:
            continue
        value = _to_decimal(decrypt_data(valuation.estimated_value_enc, master_key))
        valuations_by_asset.setdefault(valuation.asset_uuid, []).append((valued_at, value))

    account_start_date: date | None = None
    physical_assets: list[IndividualAsset] = []

    for asset in assets:
        try:
            name = decrypt_data(asset.name_enc, master_key)
        except Exception:
            name = "Actif inconnu"

        invested = _ZERO
        purchase_price_enc = getattr(asset, "purchase_price_enc", None)
        if purchase_price_enc:
            invested = _to_decimal(decrypt_data(purchase_price_enc, master_key))

        acquired_at = asset.created_at.date()
        if asset.acquisition_date_enc:
            acq_raw = decrypt_data(asset.acquisition_date_enc, master_key)
            parsed_acq = _parse_iso_date(acq_raw)
            if parsed_acq:
                acquired_at = parsed_acq

        if account_start_date is None or acquired_at < account_start_date:
            account_start_date = acquired_at

        sold_at: date | None = None
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
            account_created_at=account_start_date or min(a.created_at.date() for a in assets),
            physical_assets=physical_assets,
            total_invested=sum(a.invested for a in physical_assets),
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
            stock_accounts = _build_stock_snapshots(session, master_key, user_uuid_bidx)
            all_accounts += stock_accounts
        except Exception as exc:
            logger.warning("account_history: stock snapshot error: %s", exc)
            session.rollback()

        try:
            show_negative = getattr(settings, "crypto_show_negative_positions", False)
            crypto_accounts = _build_crypto_snapshots(
                session, master_key, user_uuid_bidx, show_negative
            )
            all_accounts += crypto_accounts
        except Exception as exc:
            logger.warning("account_history: crypto snapshot error: %s", exc)
            session.rollback()

        try:
            bank_accounts = _build_bank_snapshots(session, master_key, user_uuid_bidx)
            all_accounts += bank_accounts
        except Exception as exc:
            logger.warning("account_history: bank snapshot error: %s", exc)
            session.rollback()

        try:
            asset_accounts = _build_asset_snapshots(session, master_key, user_uuid_bidx)
            all_accounts += asset_accounts
        except Exception as exc:
            logger.warning("account_history: asset snapshot error: %s", exc)
            session.rollback()

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
                    session.commit()
                    first_and_last = None

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
                    if sym and getattr(tx, "type", "") not in ("DEPOSIT", "ANCHOR") and sym != "EUR":
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
                prev_value = _to_decimal(decrypt_data(last_row.total_value_enc, master_key))
            else:
                prev_value = _ZERO

            rows = _generate_missing_snapshots(
                session=session,
                user_uuid_bidx=user_uuid_bidx,
                account_id_bidx=account_id_bidx,
                account_snapshot=acc_snap,
                price_matrix=price_matrix,
                missing_dates=missing_dates,
                prev_value=prev_value,
                has_previous_snapshot=last_row is not None,
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
    symbols: list[str] | None = None,
    asset_type: AssetType | None = None,
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
        logger.info(
            "account_history: invalidated %d snapshot(s) from %s for account_bidx=%s",
            deleted.rowcount,
            from_date,
            account_id_bidx[:8] + "…",
        )

        if symbols and asset_type is not None:
            for symbol in symbols:
                ensure_price_history(session, symbol, asset_type, from_date)
        session.commit()
    run_lazy_catchup(user_uuid, master_key)


def trigger_post_transaction_updates(
    session: Session,
    background_tasks: "BackgroundTasks", # type: ignore
    user_uuid: str,
    master_key: str,
    asset_type: AssetType,
    affected_dates: list[date | None],
    affected_assets: list[str | None],
    account_id: str | None = None,
    account_id_bidx: str | None = None,
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

