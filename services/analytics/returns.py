"""Time-weighted and money-weighted returns.

These two answer different questions and their difference is the point: TWR is
what the strategy returned, MWR is what the investor actually earned. The gap
between them is the behavioural signal (Dichev 2007; Morningstar "Mind the Gap").

Decimal is the storage type, but the XIRR solver works in float: Decimal has no
fractional power, and a rate solved to 1e-9 is far past what two years of retail
data can support anyway.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

_ZERO = Decimal("0")
_DAYS_PER_YEAR = Decimal("365")

# Bracket for the bisection. Below -99% a portfolio is gone; above 1000% a year
# the flows are not a return series any more.
_RATE_LOW = -0.99
_RATE_HIGH = 10.0
_BISECTION_STEPS = 200


@dataclass(frozen=True)
class TwrResult:
    total_return: Decimal | None
    days: int
    skipped_days: int


def time_weighted_return(
    series: Sequence[tuple[date, Decimal]],
    flows: Mapping[date, Decimal],
) -> TwrResult:
    """Chain daily returns, neutralising external cash flows.

    Start-of-day convention (daily Modified Dietz): a flow is assumed to land
    before the day's performance, so it belongs in the denominator. Days whose
    base is not positive carry no measurable return and are skipped rather than
    counted as zero — zeroing would silently dilute the chain.
    """
    if len(series) < 2:
        return TwrResult(None, 0, 0)

    ordered = sorted(series, key=lambda point: point[0])
    growth = Decimal("1")
    days = 0
    skipped = 0

    for (_, previous_value), (day, value) in zip(ordered, ordered[1:], strict=False):
        flow = Decimal(str(flows.get(day, _ZERO)))
        base = previous_value + flow
        if base <= _ZERO:
            skipped += 1
            continue
        growth *= Decimal("1") + (value - previous_value - flow) / base
        days += 1

    if days == 0:
        return TwrResult(None, 0, skipped)
    return TwrResult(growth - Decimal("1"), days, skipped)


def _npv(rate: float, cashflows: Sequence[tuple[date, Decimal]], origin: date) -> float:
    total = 0.0
    for day, amount in cashflows:
        years = (day - origin).days / 365.0
        total += float(amount) / ((1.0 + rate) ** years)
    return total


def xirr(cashflows: Sequence[tuple[date, Decimal]]) -> Decimal | None:
    """Money-weighted return, solved by bisection.

    Bisection over a bracketed sign change always converges. Newton does not: the
    irregular flow patterns a retail ledger produces routinely send it outside the
    bracket. Returns None when no rate solves the flows.
    """
    if len(cashflows) < 2:
        return None

    origin = min(day for day, _ in cashflows)
    low_npv = _npv(_RATE_LOW, cashflows, origin)
    high_npv = _npv(_RATE_HIGH, cashflows, origin)
    if low_npv * high_npv > 0:
        return None

    low, high = _RATE_LOW, _RATE_HIGH
    for _ in range(_BISECTION_STEPS):
        middle = (low + high) / 2
        if _npv(low, cashflows, origin) * _npv(middle, cashflows, origin) <= 0:
            high = middle
        else:
            low = middle

    return Decimal(str((low + high) / 2))


def annualize(total_return: Decimal, days: int) -> Decimal | None:
    """Geometric annualisation. None for a window too short to mean anything.

    The caller is responsible for gating this: under three years an annualised
    figure is arithmetically valid and statistically weak, and the spec requires
    it be labelled as such rather than hidden.
    """
    if days <= 0 or total_return is None or total_return <= Decimal("-1"):
        return None
    years = Decimal(days) / _DAYS_PER_YEAR
    annual = (1.0 + float(total_return)) ** (1.0 / float(years)) - 1.0
    return Decimal(str(annual))
