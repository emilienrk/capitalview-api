"""Adherence to a declared plan — the hardest comparison on the page.

Everything else compares the investor with a benchmark or with chance. This one
compares what they wrote down with what they did.

The plan is optional and this module is a no-op without one. It carries a
`since` month the design did not foresee but the data demands: a plan declared
today, applied backwards over three years, produces a damning adherence figure
about months during which no plan existed.

A plan also *changes* — income rises, and the amount and the allocation rise
with it. So a plan is a list of periods, each in force from its own month until
the next one starts. A single-period plan is stored flat and reads exactly as it
did before; the shape of what is stored says which it is, so no mode has to be
persisted alongside it. Every complete month is then scored against the target
in force *that month*, which is the only way a raise does not read as three
years of under-investment.
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
class PlanPeriod:
    """One stretch of the plan: a monthly amount and an allocation, from a month."""

    since: date
    monthly_target: Decimal
    allocation: dict[str, Decimal]


@dataclass(frozen=True)
class PeriodOutcome:
    """What one period asked for, and what was actually done while it was in force.

    Drift compares the portfolio *today* against the target in force today, so it
    cannot say anything about a period that has ended: two years of market moves
    sit between the purchases and the current weights. This compares the flows
    instead — the euros put in during the period, split by line, against the split
    the period asked for. That is the only figure that survives a plan revision,
    and the only one that answers "did I follow 50/50 while 50/50 was the plan".
    """

    since: date
    until: date | None
    """First month of the next period, or None while this one is still running."""
    monthly_target: Decimal
    allocation: dict[str, Decimal]
    months: int
    target_eur: Decimal
    invested_eur: Decimal
    flow_shares: dict[str, Decimal]
    """Share of the period's euros that went to each line, in points."""
    flow_drift_l1: Decimal | None
    """L1 distance between those shares and the target, in points."""

    @property
    def adherence_ratio(self) -> Decimal | None:
        return self.invested_eur / self.target_eur if self.target_eur > _ZERO else None


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
    """The target in force today — the last period's, when the plan is split."""
    since: date
    """The month the plan starts, which is the first period's."""
    periods: list[PlanPeriod]
    outcomes: list[PeriodOutcome]
    """Per period: what was promised, and what was actually put in while it ran."""
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


def parse_plan(raw, default_since: date | None = None) -> list[PlanPeriod] | None:
    """Validate a stored plan into its periods, or explain why it cannot be used.

    An allocation that does not add up is rejected rather than normalised: a
    silent rescale would score the user against a plan they never wrote.

    Both storage shapes land here. A plan that never changed is a flat object
    and becomes a single period; one that did carries `periods` and becomes as
    many. Periods come back sorted by month, so the caller never has to trust
    the order they were stored in.
    """
    if not raw:
        return None

    stored = raw.get("periods")
    if stored is None:
        return [_parse_period(raw, default_since=default_since, label="")]

    if not isinstance(stored, list) or not stored:
        raise PlanError("Le plan déclare des périodes mais n'en contient aucune.")

    periods = [
        _parse_period(
            entry,
            # Only the opening period may leave its month implicit: the others
            # exist precisely because they start somewhere.
            default_since=default_since if index == 0 else None,
            label=f"Période {index + 1} : ",
        )
        for index, entry in enumerate(stored)
    ]
    periods.sort(key=lambda period: period.since)

    months = [period.since for period in periods]
    if len(set(months)) != len(months):
        raise PlanError("Deux périodes du plan démarrent le même mois.")
    return periods


def _parse_period(raw, *, default_since: date | None, label: str) -> PlanPeriod:
    """Validate one period. `label` names it when the plan has several."""
    if not isinstance(raw, dict):
        raise PlanError(f"{label}la période n'a pas la forme attendue.")

    try:
        monthly = Decimal(str(raw.get("monthly_target") or "0"))
    except (InvalidOperation, TypeError):
        raise PlanError(f"{label}le montant mensuel n'est pas un nombre.")
    if monthly <= _ZERO:
        raise PlanError(f"{label}le montant mensuel doit être supérieur à zéro.")

    allocation: dict[str, Decimal] = {}
    for key, value in (raw.get("allocation") or {}).items():
        try:
            share = Decimal(str(value))
        except (InvalidOperation, TypeError):
            raise PlanError(f"{label}l'allocation de {key} n'est pas un nombre.")
        if share < _ZERO:
            raise PlanError(f"{label}l'allocation de {key} est négative.")
        allocation[str(key).upper()] = share

    if allocation:
        total = sum(allocation.values())
        if abs(total - _HUNDRED) > ALLOCATION_TOLERANCE:
            raise PlanError(
                f"{label}ton allocation cible fait {total} % au lieu de 100 %. "
                "Corrige-la plutôt que de la laisser être normalisée en silence."
            )

    since = _parse_month(raw.get("since")) or default_since
    if since is None:
        raise PlanError(f"{label}le mois de départ est inconnu.")
    return PlanPeriod(since=since, monthly_target=monthly, allocation=allocation)


def _parse_month(value) -> date | None:
    if not value:
        return None
    text = str(value)
    try:
        year, month = text.split("-")[:2]
        return date(int(year), int(month), 1)
    except (ValueError, IndexError):
        return None


def target_in_force(periods: list[PlanPeriod], year: int, month: int) -> Decimal:
    """The monthly amount promised for a given month.

    The last period that had started by then, which for a single-period plan is
    simply that period. Months before the plan begins never reach this function:
    they are not scored at all.
    """
    target = periods[0].monthly_target
    for period in periods:
        if (period.since.year, period.since.month) <= (year, month):
            target = period.monthly_target
        else:
            break
    return target


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
    periods = parse_plan(raw_plan, default_since=date(window.start.year, window.start.month, 1))
    if periods is None:
        return None

    since = periods[0].since
    current = periods[-1]

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
            target=target_in_force(periods, year, month),
            invested=invested_by_month.get((year, month), _ZERO),
        )
        for year, month in months
    ]

    total_target = sum((row.target for row in rows), _ZERO)
    total_invested = sum((row.invested for row in rows), _ZERO)
    ratio = total_invested / total_target if total_target > _ZERO else None
    average = total_invested / Decimal(len(rows)) if rows else None

    # Drift reads the period in force today: it says what to rebalance towards
    # now, and an allocation abandoned two years ago is not that.
    drift, drift_l1, rebalance = _allocation_drift(current.allocation, weights, portfolio_value)

    under = [row for row in rows if row.invested < row.target]
    down = _down_months(benchmark_series or {})
    under_in_down = sum(1 for row in under if (row.year, row.month) in down)

    return PlanAdherence(
        monthly_target=current.monthly_target,
        since=since,
        periods=periods,
        outcomes=_period_outcomes(periods, purchases_by_asset, rows),
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


def _period_outcomes(
    periods: list[PlanPeriod],
    purchases: list[tuple[date, str, Decimal]],
    rows: list[MonthlyAdherence],
) -> list[PeriodOutcome]:
    """Score every period on its own months and its own allocation.

    Only complete months count, exactly as the headline figure does — a period
    scored over a month still running would show a shortfall that resolves
    itself, and the two numbers would then disagree on the same data.
    """
    scored_months = {(row.year, row.month) for row in rows}
    outcomes: list[PeriodOutcome] = []

    for index, period in enumerate(periods):
        nxt = periods[index + 1].since if index + 1 < len(periods) else None
        months = [
            row
            for row in rows
            if (row.year, row.month) >= (period.since.year, period.since.month)
            and (nxt is None or (row.year, row.month) < (nxt.year, nxt.month))
        ]

        flows: dict[str, Decimal] = {}
        for day, key, amount in purchases:
            if (day.year, day.month) not in scored_months:
                continue
            if day < period.since or (nxt is not None and day >= nxt):
                continue
            flows[key] = flows.get(key, _ZERO) + amount

        invested = sum(flows.values(), _ZERO)
        shares = (
            {key: (amount / invested) * _HUNDRED for key, amount in flows.items()}
            if invested > _ZERO
            else {}
        )
        drift = None
        if period.allocation and shares:
            keys = {*period.allocation, *shares}
            drift = sum(
                (abs(shares.get(key, _ZERO) - period.allocation.get(key, _ZERO)) for key in keys),
                _ZERO,
            )

        outcomes.append(
            PeriodOutcome(
                since=period.since,
                until=nxt,
                monthly_target=period.monthly_target,
                allocation=dict(period.allocation),
                months=len(months),
                target_eur=period.monthly_target * Decimal(len(months)),
                invested_eur=invested,
                flow_shares=shares,
                flow_drift_l1=drift,
            )
        )
    return outcomes


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
