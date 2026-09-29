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
    *,
    ensure: bool = True,
) -> dict[date, Decimal]:
    """Daily EUR price per calendar day, forward-filled across closed sessions.

    Forward-filling is what makes the series alignable with the portfolio's daily
    snapshots, which exist every calendar day including weekends. The window
    starts on the user's own history, so it rarely opens on a trading day —
    fill_price_gaps seeds it from the last quote before the window, which is why
    this shares the snapshot rebuild's implementation rather than rolling its own.
    """
    # Backfilling is a network round trip. A caller that already ensured this
    # asset over the same window opts out rather than paying for it twice.
    if ensure:
        ensure_price_history(session, asset_key, AssetType.STOCK, from_date)

    if to_date < from_date:
        return {}

    days = [from_date + timedelta(days=n) for n in range((to_date - from_date).days + 1)]
    matrix = get_price_matrix(session, [asset_key], from_date, to_date)
    filled = fill_price_gaps(matrix, [asset_key], days, session)
    return {day: Decimal(str(price)) for day, price in sorted(filled.get(asset_key, {}).items())}


def user_benchmark(session: Session, user_uuid: str, master_key: str) -> tuple[str, str]:
    """The user's benchmark key and the name to print for it."""
    from services.analytics.labels import label_of, resolve_asset_labels
    from services.settings import get_or_create_settings

    key = resolve_benchmark_key(get_or_create_settings(session, user_uuid, master_key))
    return key, label_of(resolve_asset_labels(session, [key]), key).name


def total_return(series: dict[date, Decimal], start: date, end: date) -> Decimal | None:
    """Accumulating ETF: first and last quote are the whole total return."""
    first, last = series.get(start), series.get(end)
    if not first or not last or first <= Decimal("0"):
        return None
    return last / first - Decimal("1")


def benchmark_return(session: Session, asset_key: str, start: date, end: date) -> Decimal | None:
    return total_return(get_benchmark_series(session, asset_key, start, end), start, end)
