"""Benchmark price series for the counterfactual comparisons.

The default is an accumulating MSCI World ETF, and accumulating is a constraint
rather than a taste: it reinvests dividends internally, so its raw quoted price
is already a total-return series. A distributing benchmark would need dividend
data this app deliberately does not store per asset.
"""

from datetime import date, timedelta
from decimal import Decimal

from sqlmodel import Session

from models.enums import AssetType
from services.analytics.prices import fill_price_gaps, get_price_matrix
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
    snapshots, which exist every calendar day including weekends. The window
    starts on the user's own history, so it rarely opens on a trading day —
    fill_price_gaps seeds it from the last quote before the window, which is why
    this shares the snapshot rebuild's implementation rather than rolling its own.
    """
    ensure_price_history(session, asset_key, AssetType.STOCK, from_date)

    if to_date < from_date:
        return {}

    days = [from_date + timedelta(days=n) for n in range((to_date - from_date).days + 1)]
    matrix = get_price_matrix(session, [asset_key], from_date, to_date)
    filled = fill_price_gaps(matrix, [asset_key], days, session)
    return {day: Decimal(str(price)) for day, price in sorted(filled.get(asset_key, {}).items())}
