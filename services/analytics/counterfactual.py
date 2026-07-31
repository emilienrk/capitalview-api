"""The counterfactual bridge — what each decision cost, in euros.

A waterfall from a mechanical baseline to the real portfolio, replacing one
decision at a time. The baseline is a robot: the same capital actually invested,
spread in equal monthly purchases on the benchmark, no fees, no idle cash.

The decomposition follows Brinson-style attribution in spirit only. Canonical
Brinson splits allocation and selection over sector segments, which would need an
ETF look-through this app does not have. Here the segmentation is over
*decisions*, not sectors — an adaptation, and the UI says so.

Two properties matter more than the individual numbers:

- **It reconciles exactly.** The terms plus the residual must equal the real
  portfolio value. Anything left over surfaces as an explicit "unexplained" bar
  and is never absorbed into a neighbouring term to make the chart look tidy.
- **It is path dependent.** The substitution order is a choice; reordering moves
  a few points between adjacent terms. The order is part of the output so the UI
  can state it rather than imply a canonical decomposition exists.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from services.analytics.execution import month_quotes
from services.analytics.window import AnalysisWindow

_ZERO = Decimal("0")

# Below a year the benchmark replay says more about the entry point than about
# behaviour, and an attributed euro figure would read as far more precise than it is.
MIN_COVERED_DAYS = 365


@dataclass(frozen=True)
class BridgeStep:
    key: str
    label: str
    amount: Decimal


@dataclass(frozen=True)
class Bridge:
    baseline: Decimal
    steps: list[BridgeStep]
    residual: Decimal
    final: Decimal
    idle_cash: Decimal
    idle_cash_opportunity: Decimal | None
    covered_from: date
    covered_days: int
    truncated: bool
    order: list[str]

    @property
    def behaviour_cost(self) -> Decimal:
        """Everything between the robot and the real portfolio.

        Both ends carry the same uninvested cash, so this is the sum of the
        decision terms and nothing else. Letting idle cash into this number would
        make a large untouched deposit read as brilliant investing.
        """
        return self.final - self.baseline


def _tx_type(tx) -> str:
    raw = getattr(tx, "type", None)
    return str(getattr(raw, "value", raw) or "")


def _tx_day(tx):
    executed_at = getattr(tx, "executed_at", None)
    return executed_at.date() if executed_at is not None else None


def _dec(value) -> Decimal:
    if value is None:
        return _ZERO
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _month_twap(quotes: dict[date, Decimal], day: date) -> Decimal | None:
    """Mean of the real daily closes of the order's calendar month.

    Shares month_quotes with execution.py on purpose: the execution term of this
    bridge and the standalone slippage block must measure the same gap, or the
    page contradicts itself between two adjacent sections.
    """
    of_month = month_quotes(quotes, day)
    if not of_month:
        return None
    return sum(_dec(p) for p in of_month.values()) / Decimal(len(of_month))


def _months_between(first: date, last: date) -> list[tuple[int, int]]:
    months = []
    year, month = first.year, first.month
    while (year, month) <= (last.year, last.month):
        months.append((year, month))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months


def build_bridge(
    window: AnalysisWindow,
    transactions,
    price_matrix: dict[str, dict[date, Decimal]],
    price_end: dict[str, Decimal],
) -> Bridge | None:
    """Attribute the gap between a mechanical baseline and the real portfolio.

    Returns None when the benchmark cannot support the comparison — too short a
    covered period, or no usable prices. Withholding beats inventing.
    """
    start = window.effective_start
    if start is None or window.end is None:
        return None

    covered_days = window.effective_days
    if covered_days < MIN_COVERED_DAYS:
        return None

    benchmark_key = window.benchmark_key
    benchmark_quotes = price_matrix.get(benchmark_key) or {}
    benchmark_end = _dec(price_end.get(benchmark_key))
    if not benchmark_quotes or benchmark_end <= _ZERO:
        return None

    scoped = [tx for tx in transactions or () if (d := _tx_day(tx)) is not None and d >= start]
    buys = [
        tx
        for tx in scoped
        if _tx_type(tx) == "BUY" and str(getattr(tx, "asset_key", "") or "").upper() != "EUR"
    ]
    if not buys:
        return None

    invested = sum(_dec(tx.amount) * _dec(tx.price_per_unit) for tx in buys)
    if invested <= _ZERO:
        return None

    # ── V0 · the robot: equal monthly purchases on the benchmark ──────────────
    buy_days = [_tx_day(tx) for tx in buys]
    months = _months_between(min(buy_days), max(buy_days))
    monthly = invested / Decimal(len(months))
    robot_units = _ZERO
    for year, month in months:
        twap = _month_twap(benchmark_quotes, date(year, month, 1))
        if twap and twap > _ZERO:
            robot_units += monthly / twap
    v0 = robot_units * benchmark_end

    # ── V1 · your real purchase calendar, still on the benchmark ──────────────
    calendar_units = _ZERO
    for tx in buys:
        twap = _month_twap(benchmark_quotes, _tx_day(tx))
        if twap and twap > _ZERO:
            calendar_units += (_dec(tx.amount) * _dec(tx.price_per_unit)) / twap
    v1 = calendar_units * benchmark_end

    # ── V2 · your real assets, still at their monthly average price ───────────
    v2 = _ZERO
    for tx in buys:
        asset_key = str(tx.asset_key).upper()
        twap = _month_twap(price_matrix.get(asset_key) or {}, _tx_day(tx))
        end_price = _dec(price_end.get(asset_key))
        if twap and twap > _ZERO:
            v2 += (_dec(tx.amount) * _dec(tx.price_per_unit)) / twap * end_price

    # ── V3 · your real execution prices: buy and hold what you actually bought ─
    bought_units: dict[str, Decimal] = {}
    for tx in buys:
        asset_key = str(tx.asset_key).upper()
        bought_units[asset_key] = bought_units.get(asset_key, _ZERO) + _dec(tx.amount)
    v3 = sum(units * _dec(price_end.get(key)) for key, units in bought_units.items())

    # ── The cash-side terms, which are what make the chain reconcile ──────────
    deposits = _ZERO
    withdrawals = _ZERO
    dividends = _ZERO
    fees_total = _ZERO
    sell_proceeds = _ZERO
    sold_units: dict[str, Decimal] = {}

    for tx in scoped:
        kind = _tx_type(tx)
        fees_total += _dec(getattr(tx, "fees", None))
        gross = _dec(getattr(tx, "amount", None)) * _dec(getattr(tx, "price_per_unit", None))
        if kind == "DEPOSIT":
            deposits += gross
        elif kind == "WITHDRAW":
            withdrawals += gross
        elif kind == "DIVIDEND":
            dividends += gross
        elif kind == "SELL":
            sell_proceeds += gross
            key = str(tx.asset_key).upper()
            sold_units[key] = sold_units.get(key, _ZERO) + _dec(tx.amount)

    buy_costs = invested
    # Money that came in and never got deployed. Negative means purchases were
    # funded by sales rather than by fresh cash.
    idle_cash = deposits - withdrawals + dividends - buy_costs
    sold_value_now = sum(units * _dec(price_end.get(key)) for key, units in sold_units.items())
    exits = sell_proceeds - sold_value_now

    steps = [
        BridgeStep("timing", "Ton calendrier d'achats", v1 - v0),
        BridgeStep("selection", "Tes actifs plutôt que l'indice", v2 - v1),
        BridgeStep("execution", "Tes prix d'exécution", v3 - v2),
        BridgeStep("fees", "Tes frais", -fees_total),
        BridgeStep("exits", "Tes ventes et arbitrages", exits),
    ]

    held_units = {
        key: units - sold_units.get(key, _ZERO) for key, units in bought_units.items()
    }
    cash_end = deposits - withdrawals + sell_proceeds + dividends - buy_costs - fees_total
    final = sum(units * _dec(price_end.get(key)) for key, units in held_units.items()) + cash_end

    # The robot is handed the same leftover cash, because it invests the capital
    # actually deployed and nothing more. Without this the comparison would credit
    # an untouched deposit as investing skill.
    baseline = v0 + idle_cash
    residual = final - (baseline + sum(step.amount for step in steps))

    return Bridge(
        baseline=baseline,
        steps=steps,
        residual=residual,
        final=final,
        idle_cash=idle_cash,
        idle_cash_opportunity=_idle_opportunity(idle_cash, benchmark_quotes, start, benchmark_end),
        covered_from=start,
        covered_days=covered_days,
        truncated=not window.benchmark_covers_window,
        order=[step.key for step in steps],
    )


def _idle_opportunity(
    idle_cash: Decimal,
    benchmark_quotes: dict[date, Decimal],
    start: date,
    benchmark_end: Decimal,
) -> Decimal | None:
    """What the uninvested cash would have earned on the benchmark.

    This is the real cost of cash drag, and it is reported next to the bridge
    rather than inside it: it is an opportunity forgone, not a euro that moved,
    so adding it to the chain would break the reconciliation it must preserve.
    """
    if idle_cash <= _ZERO or not benchmark_quotes:
        return None
    opening = _month_twap(benchmark_quotes, start)
    if not opening or opening <= _ZERO:
        return None
    return idle_cash * (benchmark_end / opening - Decimal("1"))
