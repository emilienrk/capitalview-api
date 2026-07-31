"""Execution cost — what each purchase paid versus its month's average price.

Transaction cost analysis applied to a retail ledger. For every BUY we compare
the price actually paid to the mean daily close of that asset over the calendar
month of the order.

Naming honestly: the industry benchmark is an interval VWAP, but only daily
closes are stored, with no volumes. This is therefore a **TWAP over daily
closes** and is called that everywhere, code and UI alike. The real
implementation shortfall (Perold 1988) needs a decision timestamp nobody
collects, so it is not computed and not claimed.

Amounts need no currency conversion: for stock accounts price_per_unit is in EUR
(the whole service treats amount * price_per_unit as EUR cash, see
services/stock_transaction.py), and market_price_history stores EUR too because
the backfill converts with per-date historical rates.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import numpy as np

from services.analytics.timing import (
    DEFAULT_DRAWS,
    PermutationResult,
    permutation_test,
    rng as default_rng,
)

_BUY = "BUY"
_EUR = "EUR"
_BPS = Decimal("10000")

# Under ten orders the distribution is not a distribution. Thirty is where the
# weighted mean stops being driven by one or two trades.
MIN_ORDERS = 10
SOLID_ORDERS = 30

# Beyond this median absolute gap per order, the series is not measuring
# execution any more.
#
# A retail order sits within a percent or so of its month's average price. A
# median of several hundred basis points means the price paid and the price
# stored are not the same instrument: the same ETF quoted on XFRA and on XPAR
# gives two different closes, and if the history was backfilled from a venue the
# order was not placed on, every order inherits the same offset. The permutation
# test cannot catch it — it compares days with each other, never sources with
# each other — so it would certify a systematic bias with p = 0.000.
#
# 300 bps is deliberately loose: it is a wall against a wrong instrument, not a
# judgement on execution quality.
PLAUSIBILITY_BPS = Decimal("300")


@dataclass(frozen=True)
class OrderSlippage:
    day: date
    asset_key: str
    paid: Decimal
    twap: Decimal
    notional: Decimal
    slippage_bps: Decimal


@dataclass(frozen=True)
class ExecutionAnalysis:
    orders: list[OrderSlippage]
    weighted_slippage_bps: Decimal | None
    cost_eur: Decimal | None
    quartiles: tuple[Decimal, Decimal, Decimal, Decimal, Decimal] | None
    permutation: PermutationResult | None
    median_absolute_bps: Decimal | None = None
    """How far a typical order sits from its month average, sign ignored."""

    @property
    def sample_size(self) -> int:
        return len(self.orders)

    @property
    def is_plausible(self) -> bool:
        """Whether these prices can be compared at all.

        False means the prices paid and the prices stored most likely come from
        different quote venues, and the block must say so instead of reporting a
        slippage. A wrong slippage is worse than no slippage.
        """
        if self.median_absolute_bps is None:
            return True
        return self.median_absolute_bps <= PLAUSIBILITY_BPS


def _tx_type(tx) -> str:
    raw = getattr(tx, "type", None)
    return str(getattr(raw, "value", raw) or "")


def _tx_day(tx):
    executed_at = getattr(tx, "executed_at", None)
    return executed_at.date() if executed_at is not None else None


def buy_orders(transactions) -> list:
    """Real purchases only — EUR cash rows are not investment decisions."""
    return [
        tx
        for tx in transactions or ()
        if _tx_type(tx) == _BUY
        and str(getattr(tx, "asset_key", "") or "").upper() != _EUR
        and _tx_day(tx) is not None
    ]


def month_quotes(quotes: dict[date, Decimal], day: date) -> dict[date, Decimal]:
    """Quotes of the calendar month containing `day`.

    Built from the sparse matrix on purpose: forward-filled values repeat a
    previous session's close, and averaging them would weight a single quote by
    how many days the market stayed shut afterwards.
    """
    return {
        quoted: price
        for quoted, price in quotes.items()
        if quoted.year == day.year and quoted.month == day.month
    }


def _twap(quotes_of_month: dict[date, Decimal]) -> Decimal | None:
    if not quotes_of_month:
        return None
    total = sum(Decimal(str(price)) for price in quotes_of_month.values())
    return total / Decimal(len(quotes_of_month))


def _slippage_bps(paid: Decimal, twap: Decimal) -> Decimal | None:
    if twap <= 0:
        return None
    return (paid - twap) / twap * _BPS


def _weighted_mean(values: list[Decimal], weights: list[Decimal]) -> Decimal | None:
    total_weight = sum(weights)
    if total_weight <= 0:
        return None
    return sum(v * w for v, w in zip(values, weights)) / total_weight


def analyse_execution(
    transactions,
    price_matrix: dict[str, dict[date, Decimal]],
    *,
    trading_days: dict[str, list[date]] | None = None,
    draws: int = DEFAULT_DRAWS,
    rng=None,
) -> ExecutionAnalysis:
    """Slippage per order against its month's TWAP, plus a permutation test.

    The permutation re-dates each order uniformly among the trading days of its
    own month, keeping the asset and the amount fixed. That cancels exactly one
    thing — the choice of day within the month — and freezes everything else,
    which is what separates a systematic execution bias from a run of luck.
    """
    generator = rng if rng is not None else default_rng()
    orders: list[OrderSlippage] = []
    candidate_prices: list[list[Decimal]] = []

    for tx in buy_orders(transactions):
        asset_key = str(tx.asset_key).upper()
        quotes = price_matrix.get(asset_key) or {}
        day = _tx_day(tx)
        of_month = month_quotes(quotes, day)
        twap = _twap(of_month)
        if twap is None:
            continue

        paid = Decimal(str(getattr(tx, "price_per_unit", 0) or 0))
        bps = _slippage_bps(paid, twap)
        if bps is None:
            continue

        notional = Decimal(str(getattr(tx, "amount", 0) or 0)) * paid
        orders.append(
            OrderSlippage(
                day=day,
                asset_key=asset_key,
                paid=paid,
                twap=twap,
                notional=notional,
                slippage_bps=bps,
            )
        )

        allowed = _allowed_days(asset_key, day, of_month, trading_days)
        candidate_prices.append([of_month[d] for d in allowed if d in of_month] or list(of_month.values()))

    if not orders:
        return ExecutionAnalysis([], None, None, None, None)

    weights = [o.notional for o in orders]
    weighted = _weighted_mean([o.slippage_bps for o in orders], weights)
    total_notional = sum(weights)
    cost = (weighted / _BPS * total_notional) if weighted is not None else None

    return ExecutionAnalysis(
        orders=orders,
        weighted_slippage_bps=weighted,
        cost_eur=cost,
        quartiles=_quartiles([o.slippage_bps for o in orders]),
        permutation=_permute(orders, candidate_prices, weights, weighted, draws, generator),
        median_absolute_bps=_median_absolute([o.slippage_bps for o in orders]),
    )


def _median_absolute(values: list[Decimal]) -> Decimal | None:
    """Median gap per order, sign ignored — the plausibility check.

    The median rather than the mean: one order fat-fingered at the wrong price
    should not condemn a whole series, while a venue mismatch shifts every order
    at once and moves the median with them.
    """
    if not values:
        return None
    ordered = np.asarray([abs(float(v)) for v in values], dtype=np.float64)
    return Decimal(str(round(float(np.median(ordered)), 4)))


def _allowed_days(
    asset_key: str,
    day: date,
    of_month: dict[date, Decimal],
    trading_days: dict[str, list[date]] | None,
) -> list[date]:
    """Days the order could plausibly have been placed instead.

    Falls back to the days actually quoted for that asset when no exchange
    calendar is known — a quoted day is a traded day by definition.
    """
    if trading_days and asset_key in trading_days:
        same_month = [
            d for d in trading_days[asset_key] if d.year == day.year and d.month == day.month
        ]
        if same_month:
            return same_month
    return sorted(of_month)


def _quartiles(values: list[Decimal]):
    ordered = np.asarray([float(v) for v in values], dtype=np.float64)
    if ordered.size == 0:
        return None
    q = np.percentile(ordered, [0, 25, 50, 75, 100])
    return tuple(Decimal(str(round(float(x), 4))) for x in q)


def _permute(orders, candidate_prices, weights, observed, draws, generator):
    if observed is None or not candidate_prices:
        return None

    twaps = np.asarray([float(o.twap) for o in orders], dtype=np.float64)
    quantities = np.asarray(
        [float(o.notional / o.paid) if o.paid > 0 else 0.0 for o in orders], dtype=np.float64
    )
    if np.any(twaps <= 0):
        return None

    # Candidate pools have different lengths (months differ in trading days), so
    # they are padded into a rectangle and each column is drawn within its own
    # real size. Padding never gets picked.
    sizes = np.asarray([len(prices) for prices in candidate_prices], dtype=np.int64)
    if np.any(sizes <= 0):
        return None
    pool = np.zeros((sizes.size, int(sizes.max())), dtype=np.float64)
    for i, prices in enumerate(candidate_prices):
        pool[i, : len(prices)] = [float(p) for p in prices]

    picks = (generator.random((draws, sizes.size)) * sizes).astype(np.int64)
    np.minimum(picks, sizes - 1, out=picks)
    drawn = np.take_along_axis(pool[None, :, :], picks[:, :, None], axis=2)[:, :, 0]

    # The notional moves with the redrawn price: the investor commits a quantity,
    # not a euro amount, so a different day means a different cost.
    drawn_weights = drawn * quantities
    drawn_bps = (drawn - twaps) / twaps * 10000.0
    totals = drawn_weights.sum(axis=1)
    null = np.where(
        totals > 0, (drawn_bps * drawn_weights).sum(axis=1) / np.where(totals > 0, totals, 1.0), np.nan
    )

    return permutation_test(float(observed), null)
