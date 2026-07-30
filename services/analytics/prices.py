"""Daily price matrix for a set of assets — the single definition.

Both the snapshot rebuild and the investor analytics need "what was this asset
worth on this day". Two implementations would drift, and the analytics would
quietly stop matching the curves the app already draws.

The matrix comes out of the database sparse — markets close, and a calendar day
without a session has no row. Filling those gaps is a separate, explicit step:
callers that reason about real trading days (execution slippage) need the sparse
form, callers that walk every calendar day (snapshots, benchmark series) need the
filled one.
"""

from datetime import date
from decimal import Decimal

import sqlalchemy as sa
from sqlmodel import Session, select

from models.market import MarketAsset, MarketPriceHistory


def get_price_matrix(
    session: Session,
    asset_keys: list[str],
    from_date: date,
    to_date: date,
) -> dict[str, dict[date, Decimal]]:
    """
    Return {asset_key: {date: price}} for every (asset_key, date) pair in the range.

    Uses a single JOIN query. For dates where a price is missing, the matrix
    is left sparse — callers must handle gaps (use the nearest earlier price).
    """
    if not asset_keys:
        return {}

    rows = session.exec(
        select(MarketAsset.asset_key, MarketPriceHistory.price_date, MarketPriceHistory.price)
        .join(MarketAsset, MarketPriceHistory.market_asset_id == MarketAsset.id)
        .where(
            MarketAsset.asset_key.in_(asset_keys),
            MarketPriceHistory.price_date >= from_date,
            MarketPriceHistory.price_date <= to_date,
        )
    ).all()

    matrix: dict[str, dict[date, Decimal]] = {}
    for asset_key, price_date, price in rows:
        matrix.setdefault(asset_key, {})[price_date] = price

    return matrix


def fill_price_gaps(
    matrix: dict[str, dict[date, Decimal]],
    asset_keys: list[str],
    missing_dates: list[date],
    session: Session,
) -> dict[str, dict[date, Decimal]]:
    """
    Ensure every asset_key has a price for every date in missing_dates via forward-fill.
    For asset_keys with no price at or before missing_dates[0], a SQL fallback fetches
    the most recent known price before the range to seed the forward-fill.
    """
    if not missing_dates:
        return matrix

    first_date = missing_dates[0]

    # Seed needed: no data at all, OR earliest entry is after first_date
    asset_keys_needing_seed = [
        s for s in asset_keys
        if not matrix.get(s) or min(matrix[s].keys()) > first_date
    ]

    if asset_keys_needing_seed:
        subq = (
            sa.select(
                MarketAsset.asset_key.label("asset_key"),
                sa.func.max(MarketPriceHistory.price_date).label("max_date"),
            )
            .join(MarketAsset, MarketPriceHistory.market_asset_id == MarketAsset.id)
            .where(
                MarketAsset.asset_key.in_(asset_keys_needing_seed),
                MarketPriceHistory.price_date < first_date,
            )
            .group_by(MarketAsset.asset_key)
            .subquery()
        )

        fallback_rows = session.exec(
            sa.select(MarketAsset.asset_key, MarketPriceHistory.price)
            .join(MarketAsset, MarketPriceHistory.market_asset_id == MarketAsset.id)
            .join(
                subq,
                sa.and_(
                    MarketAsset.asset_key == subq.c.asset_key,
                    MarketPriceHistory.price_date == subq.c.max_date,
                ),
            )
        ).all()

        for asset_key, price in fallback_rows:
            matrix.setdefault(asset_key, {})[first_date] = price

    # Forward-fill across all missing_dates for every asset_key
    for asset_key in asset_keys:
        prices_for_asset_key = matrix.setdefault(asset_key, {})
        last_price: Decimal | None = None
        for d in missing_dates:
            if d in prices_for_asset_key:
                last_price = prices_for_asset_key[d]
            elif last_price is not None:
                prices_for_asset_key[d] = last_price

    return matrix
