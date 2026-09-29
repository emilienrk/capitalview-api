"""Each calendar year, pocket by pocket, beside the benchmark over the same days.

The question a year-end review asks is not how much was bought but what the
money did: the gain with deposits taken out, the time-weighted return, and what
an index fund would have returned over the very same span. The first and the
current year are partial and say so; their return is the one over the days
actually covered, never annualised into a full year's figure.
"""

import datetime
from dataclasses import dataclass, field
from decimal import Decimal

from sqlmodel import Session

from services.analytics.benchmark import get_benchmark_series, total_return, user_benchmark
from services.analytics.period import PocketInputs, PocketPeriod, load_pockets, measure_pockets

_ZERO = Decimal("0")


@dataclass
class YearPerformance:
    year: int
    start: datetime.date
    """31 December of the year before: the baseline the year is measured from."""
    end: datetime.date
    covered_from: datetime.date
    """1 January, or the first day anything was held when that came later."""
    complete: bool
    pockets: dict[str, PocketPeriod]
    gain: Decimal
    net_contributions: Decimal
    benchmark_return: Decimal | None = None
    benchmark_start: datetime.date | None = None
    benchmark_end: datetime.date | None = None


@dataclass
class YearlyPerformance:
    benchmark_asset_key: str
    benchmark_name: str
    years: list[YearPerformance] = field(default_factory=list)


def _held(pocket: PocketPeriod) -> bool:
    """A placement opened in 2025 still answers zeros for 2022: that year it did not exist."""
    return pocket.gain is not None and bool(
        pocket.value_start or pocket.value_end or pocket.net_contributions
    )


def measure_years(
    inputs: PocketInputs,
    benchmark: dict[datetime.date, Decimal],
    today: datetime.date,
) -> list[YearPerformance]:
    """One entry per calendar year that has something measured, oldest first."""
    first_day = inputs.first_day()
    if first_day is None or first_day > today:
        return []

    years = []
    for year in range(first_day.year, today.year + 1):
        start = datetime.date(year - 1, 12, 31)
        end = min(datetime.date(year, 12, 31), today)
        pockets = {
            name: pocket for name, pocket in measure_pockets(inputs, start, end).items()
            if _held(pocket)
        }
        if not pockets:
            continue

        # The comparison runs over the stock pocket's own days: a PEA opened in
        # June is set against the index from June, not from January.
        stocks = pockets.get("stocks")
        bench_start = stocks.start if stocks and stocks.start and stocks.start > start else start
        bench_end = stocks.end if stocks and stocks.end else end
        bench = total_return(benchmark, bench_start, bench_end)

        years.append(
            YearPerformance(
                year=year,
                start=start,
                end=end,
                covered_from=max(start + datetime.timedelta(days=1), first_day),
                complete=end == datetime.date(year, 12, 31) and first_day <= start,
                pockets=pockets,
                gain=sum((pocket.gain for pocket in pockets.values()), _ZERO),
                net_contributions=sum((pocket.net_contributions for pocket in pockets.values()), _ZERO),
                benchmark_return=bench,
                benchmark_start=bench_start if bench is not None else None,
                benchmark_end=bench_end if bench is not None else None,
            )
        )
    return years


def yearly_performance(
    session: Session, user_uuid: str, master_key: str, today: datetime.date | None = None
) -> YearlyPerformance:
    today = today or datetime.date.today()
    benchmark_key, name = user_benchmark(session, user_uuid, master_key)

    inputs = load_pockets(session, user_uuid, master_key)
    first_day = inputs.first_day()
    if first_day is None:
        return YearlyPerformance(benchmark_key, name)

    benchmark = get_benchmark_series(
        session, benchmark_key, datetime.date(first_day.year - 1, 12, 31), today
    )
    return YearlyPerformance(benchmark_key, name, measure_years(inputs, benchmark, today))


def _pocket_payload(pocket: PocketPeriod) -> dict:
    return {
        "start": pocket.start,
        "end": pocket.end,
        "value_start": pocket.value_start,
        "value_end": pocket.value_end,
        "net_contributions": pocket.net_contributions,
        "gain": pocket.gain,
        "time_weighted_return": pocket.time_weighted_return,
        "notes": pocket.warnings,
    }


def yearly_payload(result: YearlyPerformance) -> dict:
    return {
        "benchmark_asset_key": result.benchmark_asset_key,
        "benchmark_name": result.benchmark_name,
        "years": [
            {
                "year": year.year,
                "start": year.start,
                "end": year.end,
                "covered_from": year.covered_from,
                "complete": year.complete,
                **{name: _pocket_payload(pocket) for name, pocket in year.pockets.items()},
                "gain": year.gain,
                "net_contributions": year.net_contributions,
                "benchmark_return": year.benchmark_return,
                "benchmark_start": year.benchmark_start,
                "benchmark_end": year.benchmark_end,
            }
            for year in result.years
        ],
    }
