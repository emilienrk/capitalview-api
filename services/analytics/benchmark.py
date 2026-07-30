"""Benchmark price series for the counterfactual comparisons.

The default is an accumulating MSCI World ETF, and accumulating is a constraint
rather than a taste: it reinvests dividends internally, so its raw quoted price
is already a total-return series. A distributing benchmark would need dividend
data this app deliberately does not store per asset.
"""

from datetime import date, timedelta
from decimal import Decimal

from sqlmodel import Session, select

from models.enums import AssetType
from models.market import MarketAsset, MarketPriceHistory
from services.market import ensure_price_history

# iShares Core MSCI World UCITS ETF USD (Acc) — IWDA.
DEFAULT_BENCHMARK_ASSET_KEY = "IE00B4L5Y983"


def resolve_benchmark_key(settings) -> str:
    """The user's configured benchmark, or the default MSCI World."""
    key = getattr(settings, "benchmark_asset_key", None) if settings else None
    return key.strip() if key and key.strip() else DEFAULT_BENCHMARK_ASSET_KEY


def get_benchmark_series(
    session: Session,
    asset_key: str,
    from_date: date,
    to_date: date,
) -> dict[date, Decimal]:
    """Daily EUR price per calendar day, forward-filled across closed sessions.

    Forward-filling is what makes the series alignable with the portfolio's daily
    snapshots, which exist every calendar day including weekends.
    """
    ensure_price_history(session, asset_key, AssetType.STOCK, from_date)

    asset = session.exec(select(MarketAsset).where(MarketAsset.asset_key == asset_key)).first()
    if not asset:
        return {}

    rows = session.exec(
        select(MarketPriceHistory)
        .where(
            MarketPriceHistory.market_asset_id == asset.id,
            MarketPriceHistory.price_date >= from_date,
            MarketPriceHistory.price_date <= to_date,
        )
        .order_by(MarketPriceHistory.price_date)
    ).all()
    if not rows:
        return {}

    quoted = {row.price_date: Decimal(str(row.price)) for row in rows}

    series: dict[date, Decimal] = {}
    last: Decimal | None = None
    day = from_date
    while day <= to_date:
        if day in quoted:
            last = quoted[day]
        if last is not None:
            series[day] = last
        day += timedelta(days=1)
    return series
