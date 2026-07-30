"""Adherence to a declared plan — the hardest comparison on the page.

Everything else compares the investor with a benchmark or with chance. This one
compares what they wrote down with what they did.

The plan is optional and this module is a no-op without one. It carries a
`since` month the design did not foresee but the data demands: a plan declared
today, applied backwards over three years, produces a damning adherence figure
about months during which no plan existed.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

_ZERO = Decimal("0")
_HUNDRED = Decimal("100")

# Allocations rarely land exactly on 100 after rounding a few percentages.
ALLOCATION_TOLERANCE = Decimal("1")
# Three complete months before adherence says anything about a habit.
MIN_MONTHS = 3


@dataclass(frozen=True)
class MonthlyAdherence:
    year: int
    month: int
    target: Decimal
    invested: Decimal

    @property
    def gap(self) -> Decimal:
        return self.invested - self.target


@dataclass(frozen=True)
class AllocationDrift:
    asset_key: str
    target: Decimal
    actual: Decimal

    @property
    def gap(self) -> Decimal:
        return self.actual - self.target


@dataclass(frozen=True)
class PlanAdherence:
    monthly_target: Decimal
    since: date
    months: list[MonthlyAdherence]
    total_target: Decimal
    total_invested: Decimal
    adherence_ratio: Decimal | None
    """Invested over planned. Below one means under-investing."""
    average_monthly: Decimal | None
    drift: list[AllocationDrift]
    drift_l1: Decimal | None
    """L1 distance between real and target allocation, in points."""
    rebalance_eur: Decimal | None
    under_invested_months: int
    under_in_down_months: int
    """Of those, how many fell in a month the benchmark lost ground."""

    @property
    def is_measurable(self) -> bool:
        return len(self.months) >= MIN_MONTHS


class PlanError(ValueError):
    """A declared plan that cannot be scored against, with a readable reason."""


def parse_plan(raw, default_since: date | None = None) -> tuple[Decimal, dict[str, Decimal], date] | None:
    """Validate a stored plan, or explain why it cannot be used.

    An allocation that does not add up is rejected rather than normalised: a
    silent rescale would score the user against a plan they never wrote.
    """
    if not raw:
        return None

    try:
        monthly = Decimal(str(raw.get("monthly_target") or "0"))
    except (InvalidOperation, TypeError):
        raise PlanError("Le montant mensuel du plan n'est pas un nombre.")
    if monthly <= _ZERO:
        raise PlanError("Le montant mensuel du plan doit être supérieur à zéro.")

    allocation: dict[str, Decimal] = {}
    for key, value in (raw.get("allocation") or {}).items():
        try:
            share = Decimal(str(value))
        except (InvalidOperation, TypeError):
            raise PlanError(f"L'allocation de {key} n'est pas un nombre.")
        if share < _ZERO:
            raise PlanError(f"L'allocation de {key} est négative.")
        allocation[str(key).upper()] = share

    if allocation:
        total = sum(allocation.values())
        if abs(total - _HUNDRED) > ALLOCATION_TOLERANCE:
            raise PlanError(
                f"Ton allocation cible fait {total} % au lieu de 100 %. "
                "Corrige-la plutôt que de la laisser être normalisée en silence."
            )

    since = _parse_month(raw.get("since")) or default_since
    if since is None:
        raise PlanError("Le mois de départ du plan est inconnu.")
    return monthly, allocation, since


def _parse_month(value) -> date | None:
    if not value:
        return None
    text = str(value)
    try:
        year, month = text.split("-")[:2]
        return date(int(year), int(month), 1)
    except (ValueError, IndexError):
        return None


def analyse_plan(
    raw_plan,
    purchases_by_asset: list[tuple[date, str, Decimal]],
    weights: list[tuple[str, Decimal]],
    portfolio_value: Decimal,
    window,
    benchmark_series: dict | None = None,
) -> PlanAdherence | None:
    """Score the declared plan against what was actually invested."""
    if window is None or window.start is None or window.end is None:
        return None
    parsed = parse_plan(raw_plan, default_since=date(window.start.year, window.start.month, 1))
    if parsed is None:
        return None
    monthly_target, allocation, since = parsed

    # Complete months only: the current one is still running, and counting it
    # would show a shortfall every single time the page is opened.
    months = _complete_months(max(since, date(window.start.year, window.start.month, 1)), window.end)
    invested_by_month: dict[tuple[int, int], Decimal] = {}
    for day, _key, amount in purchases_by_asset:
        if day < since:
            continue
        invested_by_month[(day.year, day.month)] = (
            invested_by_month.get((day.year, day.month), _ZERO) + amount
        )

    rows = [
        MonthlyAdherence(
            year=year,
            month=month,
            target=monthly_target,
            invested=invested_by_month.get((year, month), _ZERO),
        )
        for year, month in months
    ]

    total_target = monthly_target * Decimal(len(rows))
    total_invested = sum(row.invested for row in rows)
    ratio = total_invested / total_target if total_target > _ZERO else None
    average = total_invested / Decimal(len(rows)) if rows else None

    drift, drift_l1, rebalance = _allocation_drift(allocation, weights, portfolio_value)

    under = [row for row in rows if row.invested < row.target]
    down = _down_months(benchmark_series or {})
    under_in_down = sum(1 for row in under if (row.year, row.month) in down)

    return PlanAdherence(
        monthly_target=monthly_target,
        since=since,
        months=rows,
        total_target=total_target,
        total_invested=Decimal(str(total_invested)),
        adherence_ratio=ratio,
        average_monthly=average,
        drift=drift,
        drift_l1=drift_l1,
        rebalance_eur=rebalance,
        under_invested_months=len(under),
        under_in_down_months=under_in_down,
    )


def _complete_months(first: date, last: date) -> list[tuple[int, int]]:
    months: list[tuple[int, int]] = []
    year, month = first.year, first.month
    while (year, month) < (last.year, last.month):
        months.append((year, month))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months


def _allocation_drift(allocation, weights, portfolio_value):
    """Distance between the real allocation and the target, in points.

    A held line missing from the target counts as a 0% target, and a target line
    not held counts as 0% real — otherwise drift could be hidden by omission.
    """
    if not allocation:
        return [], None, None

    actual = {key: share * _HUNDRED for key, share in weights}
    keys = sorted({*allocation, *actual})
    rows = [
        AllocationDrift(
            asset_key=key,
            target=allocation.get(key, _ZERO),
            actual=actual.get(key, _ZERO),
        )
        for key in keys
    ]
    l1 = sum(abs(row.gap) for row in rows)
    # Half the L1 distance is what has to change hands: every point sold is a
    # point bought somewhere else.
    rebalance = (l1 / Decimal("2") / _HUNDRED) * portfolio_value if portfolio_value > _ZERO else None
    return rows, Decimal(str(l1)), rebalance


def _down_months(benchmark_series: dict) -> set[tuple[int, int]]:
    """Months the benchmark ended below where it started."""
    by_month: dict[tuple[int, int], list[tuple[date, Decimal]]] = {}
    for day, price in benchmark_series.items():
        by_month.setdefault((day.year, day.month), []).append((day, price))
    down: set[tuple[int, int]] = set()
    for key, points in by_month.items():
        points.sort()
        if len(points) >= 2 and points[-1][1] < points[0][1]:
            down.add(key)
    return down
