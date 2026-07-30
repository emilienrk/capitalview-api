"""The analysis window — derived from the user's own data, never hard-coded.

Every M2 block replays the portfolio day by day, so it needs prices and exchange
rates over exactly the span the user has history for. A fixed lookback would be
wrong in both directions: too short and the oldest decisions vanish, too long and
we hammer the market API for years nobody owned anything.

The window opens on the first BUY rather than the first deposit. Depositing money
and investing it are different decisions (spec section 0 bis), and the
counterfactual compares investment decisions. Deposits re-enter only through the
cash drag term.

A benchmark is an ETF, and an ETF has an inception date. When it is younger than
the user's history the comparison simply does not exist over the uncovered part —
so the window carries that fact instead of letting a caller compute a number on a
silently truncated basis.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlmodel import Session, select

from models.enums import AssetType
from models.market import MarketAsset, MarketPriceHistory
from services.market import (
    ensure_price_history,
    get_historical_exchange_rates_db,
    get_non_trading_days,
)

_BUY = "BUY"
_EUR = "EUR"


@dataclass(frozen=True)
class AnalysisWindow:
    start: date | None
    end: date | None
    days: int
    asset_keys: list[str]
    benchmark_key: str
    benchmark_from: date | None
    benchmark_covers_window: bool
    clamped_start: date | None

    @property
    def effective_start(self) -> date | None:
        """Where a benchmark-dependent comparison may actually begin."""
        return self.clamped_start or self.start

    @property
    def effective_days(self) -> int:
        start = self.effective_start
        if start is None or self.end is None:
            return 0
        return max((self.end - start).days, 0)

    @property
    def is_empty(self) -> bool:
        return self.start is None or self.days <= 0


def _tx_type(tx) -> str:
    raw = getattr(tx, "type", None)
    return str(getattr(raw, "value", raw) or "")


def _tx_day(tx) -> date | None:
    executed_at = getattr(tx, "executed_at", None)
    return executed_at.date() if executed_at is not None else None


def first_quote_date(session: Session, asset_key: str) -> date | None:
    """Earliest stored quote for an asset, or None when it has none."""
    return session.exec(
        select(MarketPriceHistory.price_date)
        .join(MarketAsset, MarketPriceHistory.market_asset_id == MarketAsset.id)
        .where(MarketAsset.asset_key == asset_key)
        .order_by(MarketPriceHistory.price_date)
        .limit(1)
    ).first()


def resolve_window(session: Session, transactions, benchmark_key: str) -> AnalysisWindow:
    """Resolve the analysis span and make sure the market data behind it exists.

    Backfilling happens here rather than in each block: every block needs the same
    prices over the same days, and ensure_price_history is a network call.
    """
    buy_days = [
        day
        for tx in transactions or ()
        if _tx_type(tx) == _BUY and (day := _tx_day(tx)) is not None
    ]
    if not buy_days:
        return AnalysisWindow(
            start=None,
            end=None,
            days=0,
            asset_keys=[],
            benchmark_key=benchmark_key,
            benchmark_from=None,
            benchmark_covers_window=False,
            clamped_start=None,
        )

    start = min(buy_days)
    # Yesterday: today's close does not exist yet, and a partial day would make
    # the last point of every series incomparable with the others.
    end = date.today() - timedelta(days=1)
    if end < start:
        end = start

    asset_keys = sorted(
        {
            key
            for tx in transactions or ()
            if (key := str(getattr(tx, "asset_key", "") or "").upper()) and key != _EUR
        }
    )
    currencies = sorted(
        {
            currency
            for tx in transactions or ()
            if (currency := str(getattr(tx, "currency", "") or "").upper()) and currency != _EUR
        }
    )

    for asset_key in {*asset_keys, benchmark_key}:
        ensure_price_history(session, asset_key, AssetType.STOCK, start)

    # Stock prices are already stored in EUR (services/market.py converts on
    # backfill using per-date historical rates), so these rates are only needed
    # for assets that did not come through that path.
    for currency in currencies:
        get_historical_exchange_rates_db(session, currency, start, end)

    benchmark_from = first_quote_date(session, benchmark_key)
    covers = benchmark_from is not None and benchmark_from <= start
    clamped_start = None
    if benchmark_from is not None and not covers:
        clamped_start = benchmark_from

    return AnalysisWindow(
        start=start,
        end=end,
        days=(end - start).days,
        asset_keys=asset_keys,
        benchmark_key=benchmark_key,
        benchmark_from=benchmark_from,
        benchmark_covers_window=covers,
        clamped_start=clamped_start,
    )


def calendar_days(from_date: date, to_date: date) -> list[date]:
    """Every calendar day in the range, weekends included."""
    if to_date < from_date:
        return []
    return [from_date + timedelta(days=n) for n in range((to_date - from_date).days + 1)]


def resolve_trading_days(
    session: Session,
    asset_keys: list[str],
    from_date: date,
    to_date: date,
) -> dict[str, list[date]]:
    """Sessions per asset, taken from its exchange calendar.

    Quoted days are a serviceable fallback — a quote implies a session — but they
    also carry the holes of a backfill that failed for a day, and a day the market
    was open is a day the order could have been placed. Assets whose MIC is
    unknown are simply left out, so callers keep falling back to quoted days for
    them rather than being handed a wrong calendar.
    """
    if not asset_keys or to_date < from_date:
        return {}

    exchanges = session.exec(
        select(MarketAsset.asset_key, MarketAsset.exchange).where(
            MarketAsset.asset_key.in_(asset_keys)
        )
    ).all()

    days = calendar_days(from_date, to_date)
    sessions: dict[str, list[date]] = {}
    by_mic: dict[str, list[date]] = {}
    for asset_key, mic in exchanges:
        if not mic:
            continue
        if mic not in by_mic:
            closed = set(get_non_trading_days([mic], from_date, to_date))
            # An empty result means the MIC is unknown to the calendar library:
            # it never claims a range is entirely closed.
            by_mic[mic] = [d for d in days if d not in closed] if closed else []
        if by_mic[mic]:
            sessions[asset_key] = by_mic[mic]
    return sessions
