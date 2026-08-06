"""What the investor actually does, measured on purchases.

Depositing money and investing it are different decisions, and confusing them
produces a false verdict (spec section 0 bis): erratic deposits with immediate
buying is a disciplined investor, regular deposits with opportunistic buying is
market timing dressed as a plan. Everything that judges investing behaviour is
therefore computed on BUY rows. Deposits appear in one place only — the lag
between money arriving and money being put to work.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from services.analytics.flows import is_auto_provision

_BUY = "BUY"
_DEPOSIT = "DEPOSIT"
_EUR = "EUR"
_ZERO = Decimal("0")

# Six months of purchases is the least that can show a rhythm; two years is where
# a coefficient of variation stops being driven by one unusual month.
MIN_MONTHS = 6
SOLID_MONTHS = 24
MIN_PURCHASES = 3
# The day-of-month dispersion is a distribution in its own right and needs more
# than a handful of points before it means anything.
MIN_PURCHASES_FOR_DAY_OF_MONTH = 10


@dataclass(frozen=True)
class MonthlyAmount:
    year: int
    month: int
    amount: Decimal


@dataclass(frozen=True)
class PurchaseRegularity:
    monthly: list[MonthlyAmount]
    """Every month of the window, zeros included — a gap is the information."""
    months_total: int
    months_invested: int
    invested_share: Decimal | None
    variation_coefficient: Decimal | None
    longest_gap_months: int
    temporal_hhi: Decimal | None
    equivalent_monthly_purchases: Decimal | None
    day_of_month_spread: Decimal | None
    """Interquartile range of the day the month's main purchase lands on."""
    median_day_of_month: int | None
    deployment_gap: Decimal | None
    """Mean distance to a straight-line deployment, as a share of total capital.

    This is what judges regularity — the monthly figures only illustrate it.
    """
    median_gap_days: int | None
    """Median number of days between two purchases."""
    cadence_label: str
    """Descriptive, never declared: "achats autour du 6 du mois". Empty when unreadable."""
    purchase_count: int
    total_invested: Decimal

    @property
    def is_measurable(self) -> bool:
        return self.months_total >= MIN_MONTHS and self.purchase_count >= MIN_PURCHASES


def _tx_type(tx) -> str:
    raw = getattr(tx, "type", None)
    return str(getattr(raw, "value", raw) or "")


def _tx_day(tx) -> date | None:
    executed_at = getattr(tx, "executed_at", None)
    return executed_at.date() if executed_at is not None else None


def _dec(value) -> Decimal:
    if value is None:
        return _ZERO
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:
        return _ZERO


def purchases_by_asset(transactions) -> list[tuple[date, str, Decimal]]:
    """Every real purchase as (day, asset_key, euros). Cash rows are not purchases."""
    out: list[tuple[date, str, Decimal]] = []
    for tx in transactions or ():
        if _tx_type(tx) != _BUY:
            continue
        key = str(getattr(tx, "asset_key", "") or "").upper()
        if not key or key == _EUR:
            continue
        day = _tx_day(tx)
        if day is None:
            continue
        amount = _dec(getattr(tx, "amount", None)) * _dec(getattr(tx, "price_per_unit", None))
        if amount > _ZERO:
            out.append((day, key, amount))
    return sorted(out)


def purchase_amounts(transactions) -> list[tuple[date, Decimal]]:
    """Every real purchase as (day, euros). EUR cash rows are not purchases."""
    return [(day, amount) for day, _key, amount in purchases_by_asset(transactions)]


def deposit_amounts(transactions, *, include_auto_provisions: bool = False) -> list[tuple[date, Decimal]]:
    """Real external deposits as (day, euros).

    Auto-provisions are excluded by default: the app writes them one second
    before a BUY, so their date is the purchase's, not the transfer's.
    """
    out: list[tuple[date, Decimal]] = []
    for tx in transactions or ():
        if _tx_type(tx) != _DEPOSIT:
            continue
        if str(getattr(tx, "asset_key", "") or "").upper() != _EUR:
            continue
        if not include_auto_provisions and is_auto_provision(tx):
            continue
        day = _tx_day(tx)
        amount = _dec(getattr(tx, "amount", None))
        if day is not None and amount > _ZERO:
            out.append((day, amount))
    return sorted(out)


def _months_between(first: date, last: date) -> list[tuple[int, int]]:
    months: list[tuple[int, int]] = []
    year, month = first.year, first.month
    while (year, month) <= (last.year, last.month):
        months.append((year, month))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months


def _standard_deviation(values: list[Decimal], mean: Decimal) -> Decimal:
    """Population standard deviation, in Decimal.

    Population rather than sample: these months are the whole history, not a
    draw from a larger one.
    """
    variance = sum((v - mean) ** 2 for v in values) / Decimal(len(values))
    return Decimal(str(float(variance) ** 0.5))


def _quartile(ordered: list[int], fraction: float) -> Decimal:
    """Linear-interpolated quantile over a sorted list of integers."""
    if not ordered:
        return _ZERO
    position = fraction * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    weight = Decimal(str(position - low))
    return Decimal(ordered[low]) + weight * (Decimal(ordered[high]) - Decimal(ordered[low]))


def _deployment_gap(
    events: list[tuple[date, Decimal]], start: date, end: date, total: Decimal
) -> Decimal | None:
    """Mean distance between the capital actually deployed and a straight line.

    Regularity read on the cumulative capital curve rather than on calendar
    months. A strict 30-day rhythm drifts from one month to the next without the
    discipline changing, and judging it by month punished it for the calendar.

    The line joins (start, 0) to (end, total); the gap is the mean absolute
    distance to it over every day of the window, as a share of total capital, so
    two portfolios of different sizes compare directly.

    Discrete orders leave a floor of roughly 1/(2n): a few per cent is a straight
    line, not a finding — which is why the metric is capped at "indicatif".
    """
    span = (end - start).days
    if total <= _ZERO or span <= 0:
        return None

    ordered = sorted(events)
    cumulative = _ZERO
    deviation = _ZERO
    index = 0
    for offset in range(span + 1):
        day = start + timedelta(days=offset)
        while index < len(ordered) and ordered[index][0] <= day:
            cumulative += ordered[index][1]
            index += 1
        target = total * Decimal(offset) / Decimal(span)
        deviation += abs(cumulative - target)

    return deviation / (Decimal(span + 1) * total)


# A day-of-month habit repeats over a month; dividing both dispersions by their
# own cycle is what makes them comparable.
_MONTH_CYCLE = Decimal("30")


def _cadence_label(main_days: list[int], gaps: list[int]) -> tuple[str, int | None]:
    """The tighter of two readings of the rhythm, and the median interval.

    Purchases either land on the same day of the month or are spaced by a steady
    interval. Both are disciplines, and naming the looser one would describe the
    investor badly — so the smaller relative dispersion wins. Nothing here is
    declared by the user: it is read off the orders.
    """
    median_gap = int(_quartile(sorted(gaps), 0.5)) if gaps else None

    day_relative = None
    if len(main_days) >= MIN_PURCHASES_FOR_DAY_OF_MONTH:
        ordered = sorted(main_days)
        day_relative = (_quartile(ordered, 0.75) - _quartile(ordered, 0.25)) / _MONTH_CYCLE

    gap_relative = None
    if len(gaps) >= MIN_PURCHASES_FOR_DAY_OF_MONTH and median_gap:
        ordered = sorted(gaps)
        gap_relative = (_quartile(ordered, 0.75) - _quartile(ordered, 0.25)) / Decimal(median_gap)

    if day_relative is not None and (gap_relative is None or day_relative <= gap_relative):
        median_day = int(_quartile(sorted(main_days), 0.5))
        return f"achats autour du {median_day} du mois", median_gap
    if gap_relative is not None and median_gap:
        return f"achats tous les {median_gap} jours environ", median_gap
    return "", median_gap


def _series_regularity(
    events: list[tuple[date, Decimal]],
    first_month: date,
    last_month: date,
) -> PurchaseRegularity:
    months = _months_between(first_month, last_month)
    per_month: dict[tuple[int, int], Decimal] = {m: _ZERO for m in months}
    for day, amount in events:
        key = (day.year, day.month)
        if key in per_month:
            per_month[key] += amount

    amounts = [per_month[m] for m in months]
    total = sum(amounts)
    invested_months = sum(1 for a in amounts if a > _ZERO)

    invested_share = (
        Decimal(invested_months) / Decimal(len(months)) if months else None
    )

    mean = total / Decimal(len(months)) if months else _ZERO
    variation = (
        _standard_deviation(amounts, mean) / mean if mean > _ZERO else None
    )

    longest_gap = 0
    current_gap = 0
    for amount in amounts:
        if amount > _ZERO:
            current_gap = 0
        else:
            current_gap += 1
            longest_gap = max(longest_gap, current_gap)

    hhi = None
    equivalent = None
    if total > _ZERO:
        hhi = sum((a / total) ** 2 for a in amounts)
        equivalent = Decimal("1") / hhi if hhi > _ZERO else None

    # The main purchase of each month is the largest one: a 500 EUR order and a
    # 20 EUR top-up are not the same habit.
    main_days: list[int] = []
    by_month: dict[tuple[int, int], tuple[Decimal, int]] = {}
    for day, amount in events:
        key = (day.year, day.month)
        best = by_month.get(key)
        if best is None or amount > best[0]:
            by_month[key] = (amount, day.day)
    main_days = sorted(day for _, day in by_month.values())

    spread = None
    median_day = None
    if len(main_days) >= MIN_PURCHASES_FOR_DAY_OF_MONTH:
        spread = _quartile(main_days, 0.75) - _quartile(main_days, 0.25)
        median_day = int(_quartile(main_days, 0.5))

    # The rhythm read on the capital curve, which no calendar can distort.
    ordered_days = sorted(day for day, _ in events)
    gaps = [(b - a).days for a, b in zip(ordered_days, ordered_days[1:])]
    cadence, median_gap = _cadence_label(main_days, gaps)
    deployment = _deployment_gap(events, first_month, last_month, total)

    return PurchaseRegularity(
        monthly=[MonthlyAmount(y, m, per_month[(y, m)]) for y, m in months],
        months_total=len(months),
        months_invested=invested_months,
        invested_share=invested_share,
        variation_coefficient=variation,
        longest_gap_months=longest_gap,
        temporal_hhi=hhi,
        equivalent_monthly_purchases=equivalent,
        day_of_month_spread=spread,
        median_day_of_month=median_day,
        deployment_gap=deployment,
        median_gap_days=median_gap,
        cadence_label=cadence,
        purchase_count=len(events),
        total_invested=total,
    )


def analyse_purchase_regularity(transactions, window) -> PurchaseRegularity | None:
    """Monthly rhythm of the money actually invested.

    The denominator is every month of the analysis window, not just the months
    something happened: a six-month gap is the finding, and dropping empty months
    would turn an intermittent investor into a regular one.

    The temporal HHI applies the Herfindahl-Hirschman concentration index to the
    time axis of purchases. The index is standard; using it on time is this
    project's own reading, and the UI says so.
    """
    events = purchase_amounts(transactions)
    if not events or window is None or window.start is None or window.end is None:
        return None
    return _series_regularity(events, window.start, window.end)


@dataclass(frozen=True)
class Turnover:
    annual_rate: Decimal | None
    """min(purchases, sales) over average capital, per year."""
    purchases_eur: Decimal
    sales_eur: Decimal
    average_capital: Decimal
    years: Decimal

    @property
    def is_measurable(self) -> bool:
        return self.annual_rate is not None


def sale_amounts(transactions) -> list[tuple[date, Decimal]]:
    """Every real sale as (day, euros)."""
    out: list[tuple[date, Decimal]] = []
    for tx in transactions or ():
        if _tx_type(tx) != "SELL":
            continue
        if str(getattr(tx, "asset_key", "") or "").upper() == _EUR:
            continue
        day = _tx_day(tx)
        if day is None:
            continue
        amount = _dec(getattr(tx, "amount", None)) * _dec(getattr(tx, "price_per_unit", None))
        if amount > _ZERO:
            out.append((day, amount))
    return sorted(out)


def analyse_turnover(transactions, window, average_capital: Decimal) -> Turnover | None:
    """Annual portfolio turnover: min(bought, sold) over average capital.

    The minimum of the two sides, not their sum: buying and holding forever is
    not rotation, and a portfolio that only grows has a turnover of zero however
    much it buys. This is the variable Barber and Odean (2000) found to track
    underperformance, and it is reported annualised so it can be compared with
    anything published.
    """
    if window is None or window.start is None or window.end is None:
        return None

    purchases = sum(amount for _, amount in purchase_amounts(transactions))
    sales = sum(amount for _, amount in sale_amounts(transactions))
    days = (window.end - window.start).days
    years = Decimal(days) / Decimal("365") if days > 0 else _ZERO

    rate = None
    if average_capital > _ZERO and years > _ZERO:
        rate = (min(purchases, sales) / average_capital) / years

    return Turnover(
        annual_rate=rate,
        purchases_eur=Decimal(str(purchases)),
        sales_eur=Decimal(str(sales)),
        average_capital=average_capital,
        years=years,
    )


@dataclass(frozen=True)
class DepositLag:
    median_days: Decimal | None
    q1_days: Decimal | None
    q3_days: Decimal | None
    p90_days: Decimal | None
    matched_eur: Decimal
    unmatched_eur: Decimal
    """Purchase euros no real deposit could have funded."""
    never_invested_eur: Decimal
    """Deposited and still sitting there at the end of the window."""
    unpaired_deposits_eur: Decimal
    """Deposit euros the FIFO never consumed.

    The same figure as never_invested_eur today. The UI distinguishes the two —
    the FIFO leftover against deposits minus purchases — but that split would
    change a displayed amount and contradict the behaviour this module's tests
    pin down, so the engine does not make it yet.
    """
    pairs: int

    @property
    def unmatched_share(self) -> Decimal:
        total = self.matched_eur + self.unmatched_eur
        return self.unmatched_eur / total if total > _ZERO else _ZERO

    @property
    def is_measurable(self) -> bool:
        """False once most purchases cannot be traced back to a real transfer.

        Auto-provisions are dated on the purchase, so a ledger dominated by them
        hides the only thing this metric measures — how long the money waited.
        """
        return self.pairs > 0 and self.unmatched_share <= Decimal("0.5")


def _weighted_quantile(pairs: list[tuple[Decimal, Decimal]], fraction: Decimal) -> Decimal | None:
    """Quantile of (value, weight) pairs, weights being euros."""
    if not pairs:
        return None
    ordered = sorted(pairs)
    total = sum(weight for _, weight in ordered)
    if total <= _ZERO:
        return None
    target = total * fraction
    cumulative = _ZERO
    for value, weight in ordered:
        cumulative += weight
        if cumulative >= target:
            return value
    return ordered[-1][0]


def analyse_deposit_lag(transactions) -> DepositLag | None:
    """How long each deposited euro waits before it is invested.

    Deposits are matched to purchases FIFO over the cash balance: the oldest euro
    in the account is the one being spent. Only deposits dated on or before a
    purchase can fund it — anything else would manufacture a negative delay.

    Purchase euros with no eligible deposit left are *not* matched to something
    further away to make the queue balance. They are counted apart, because that
    is exactly what an auto-provisioned purchase looks like: money whose real
    arrival date the ledger never recorded.
    """
    deposits = deposit_amounts(transactions)
    purchases = purchase_amounts(transactions)
    if not purchases:
        return None

    queue: list[list] = [[day, amount] for day, amount in deposits]
    index = 0
    matched: list[tuple[Decimal, Decimal]] = []
    matched_eur = _ZERO
    unmatched_eur = _ZERO

    for buy_day, amount in purchases:
        remaining = amount
        while remaining > _ZERO and index < len(queue):
            deposit_day, available = queue[index]
            if deposit_day > buy_day:
                break
            if available <= _ZERO:
                index += 1
                continue
            used = min(available, remaining)
            queue[index][1] = available - used
            remaining -= used
            matched.append((Decimal((buy_day - deposit_day).days), used))
            matched_eur += used
            if queue[index][1] <= _ZERO:
                index += 1
        if remaining > _ZERO:
            unmatched_eur += remaining

    never_invested = sum(available for _, available in queue[index:] if available > _ZERO)

    return DepositLag(
        median_days=_weighted_quantile(matched, Decimal("0.5")),
        q1_days=_weighted_quantile(matched, Decimal("0.25")),
        q3_days=_weighted_quantile(matched, Decimal("0.75")),
        p90_days=_weighted_quantile(matched, Decimal("0.9")),
        matched_eur=matched_eur,
        unmatched_eur=unmatched_eur,
        never_invested_eur=Decimal(str(never_invested)),
        unpaired_deposits_eur=Decimal(str(never_invested)),
        pairs=len(matched),
    )


def analyse_deposit_regularity(transactions, window) -> PurchaseRegularity | None:
    """The same five indicators, on real deposits.

    Same computation on purpose: section 2.4 compares the two rhythms, and a
    comparison between two different measures would not be a comparison.
    """
    events = deposit_amounts(transactions)
    if not events or window is None or window.start is None or window.end is None:
        return None
    return _series_regularity(events, window.start, window.end)


# ── 3.2 · what you do with your exits ─────────────────────────────────

_SELL = "SELL"

# Odean's measure needs occasions, not sales: twelve is where the ratio of two
# proportions stops being three coin flips.
MIN_REALISATIONS = 12
# A hit rate under twenty closed episodes is "three winners out of five".
MIN_EPISODES = 20
# Horizon for the forward comparison, in calendar days. Fixed and stated: "the
# future return" means nothing without one.
EXIT_HORIZON_DAYS = 365


@dataclass(frozen=True)
class Episode:
    asset_key: str
    opened: date
    closed: date
    invested: Decimal
    proceeds: Decimal

    @property
    def profit(self) -> Decimal:
        return self.proceeds - self.invested


@dataclass(frozen=True)
class Exits:
    realised_gains: int
    realised_losses: int
    paper_gains: int
    paper_losses: int
    pgr: Decimal | None
    plr: Decimal | None
    ratio: Decimal | None
    """PGR/PLR. Above one means gains are cut and losses are kept."""
    unpriced: int
    """Sale days a position could not be valued on — reported, not ignored."""
    cost_eur: Decimal | None
    """What the sales gave up against the benchmark over the horizon."""
    measured_sales: int
    recent_sales: int
    """Sales too recent to have a full horizon: excluded, never measured short."""
    episodes: list[Episode]
    hit_rate: Decimal | None
    payoff_ratio: Decimal | None

    @property
    def realisations(self) -> int:
        return self.realised_gains + self.realised_losses

    @property
    def is_measurable(self) -> bool:
        return self.realisations >= MIN_REALISATIONS

    @property
    def has_episodes(self) -> bool:
        return len(self.episodes) >= MIN_EPISODES


def _price_on(quotes: dict[date, Decimal], day: date) -> Decimal | None:
    """Last quote at or before a day. Sales happen on closed days too."""
    if not quotes:
        return None
    if day in quotes:
        return quotes[day]
    earlier = [d for d in quotes if d <= day]
    return quotes[max(earlier)] if earlier else None


def _forward_return(quotes: dict[date, Decimal], start: date, horizon: date) -> Decimal | None:
    first = _price_on(quotes, start)
    last = _price_on(quotes, horizon)
    if first is None or last is None or first <= _ZERO:
        return None
    return last / first - Decimal("1")


def analyse_exits(transactions, price_matrix, benchmark_series=None, today=None) -> Exits | None:
    """Everything the ledger says about how positions are closed.

    Three readings of one behaviour, under one gate:

    - **PGR/PLR** (Odean 1998), the canonical frequency measure. Cost basis is the
      weighted average, matching what the rest of the app calls a realised gain —
      a different basis here would make this page contradict the account summary
      on the very same sales.
    - **The euro cost**, because a ratio is not actionable. Each sale is compared
      with the benchmark over a fixed one-year horizon; sales too recent for that
      horizon are excluded and counted rather than measured over three weeks.
    - **Closed episodes**, a full round trip from first purchase to complete
      exit, giving a hit rate and a payoff ratio.
    """
    today = today or date.today()
    ordered = sorted(
        (tx for tx in transactions or () if _tx_day(tx) is not None),
        key=lambda tx: getattr(tx, "executed_at"),
    )
    if not ordered:
        return None

    positions: dict[str, dict] = {}
    realised_gains = realised_losses = paper_gains = paper_losses = 0
    unpriced = 0
    cost = _ZERO
    measured_sales = 0
    recent_sales = 0
    episodes: list[Episode] = []
    benchmark = benchmark_series or {}

    for tx in ordered:
        key = str(getattr(tx, "asset_key", "") or "").upper()
        if not key or key == _EUR:
            continue
        day = _tx_day(tx)
        quantity = _dec(getattr(tx, "amount", None))
        price = _dec(getattr(tx, "price_per_unit", None))
        kind = _tx_type(tx)

        if kind == _BUY:
            position = positions.setdefault(
                key,
                {"quantity": _ZERO, "cost": _ZERO, "opened": day, "invested": _ZERO, "proceeds": _ZERO},
            )
            if position["quantity"] <= _ZERO:
                # A line bought back after a full exit opens a new episode.
                position["opened"] = day
            position["quantity"] += quantity
            position["cost"] += quantity * price
            position["invested"] += quantity * price
            continue

        if kind != _SELL:
            continue

        position = positions.get(key)
        if position is None or position["quantity"] <= _ZERO:
            continue

        average_cost = position["cost"] / position["quantity"]
        sold = min(quantity, position["quantity"])

        # Realised: this line, on this day. Available but not realised: every
        # other line held that day, valued at its own quote.
        if price > average_cost:
            realised_gains += 1
        elif price < average_cost:
            realised_losses += 1

        for other_key, other in positions.items():
            if other_key == key or other["quantity"] <= _ZERO:
                continue
            quote = _price_on(price_matrix.get(other_key) or {}, day)
            if quote is None:
                unpriced += 1
                continue
            other_average = other["cost"] / other["quantity"]
            if quote > other_average:
                paper_gains += 1
            elif quote < other_average:
                paper_losses += 1

        horizon = day + timedelta(days=EXIT_HORIZON_DAYS)
        if horizon > today:
            recent_sales += 1
        else:
            asset_forward = _forward_return(price_matrix.get(key) or {}, day, horizon)
            benchmark_forward = _forward_return(benchmark, day, horizon)
            if asset_forward is not None and benchmark_forward is not None:
                # Positive means the sold line went on to beat the index: the exit
                # gave that difference up.
                cost += (asset_forward - benchmark_forward) * sold * price
                measured_sales += 1

        position["quantity"] -= sold
        position["cost"] -= average_cost * sold
        position["proceeds"] += sold * price
        if position["quantity"] <= _ZERO:
            episodes.append(
                Episode(
                    asset_key=key,
                    opened=position["opened"],
                    closed=day,
                    invested=position["invested"],
                    proceeds=position["proceeds"],
                )
            )
            position["quantity"] = _ZERO
            position["cost"] = _ZERO
            position["invested"] = _ZERO
            position["proceeds"] = _ZERO

    pgr = _proportion(realised_gains, paper_gains)
    plr = _proportion(realised_losses, paper_losses)
    ratio = pgr / plr if pgr is not None and plr is not None and plr > _ZERO else None

    winners = [e for e in episodes if e.profit > _ZERO]
    losers = [e for e in episodes if e.profit < _ZERO]
    hit_rate = Decimal(len(winners)) / Decimal(len(episodes)) if episodes else None
    payoff = None
    if winners and losers:
        average_win = sum(e.profit for e in winners) / Decimal(len(winners))
        average_loss = abs(sum(e.profit for e in losers) / Decimal(len(losers)))
        payoff = average_win / average_loss if average_loss > _ZERO else None

    return Exits(
        realised_gains=realised_gains,
        realised_losses=realised_losses,
        paper_gains=paper_gains,
        paper_losses=paper_losses,
        pgr=pgr,
        plr=plr,
        ratio=ratio,
        unpriced=unpriced,
        cost_eur=cost if measured_sales else None,
        measured_sales=measured_sales,
        recent_sales=recent_sales,
        episodes=episodes,
        hit_rate=hit_rate,
        payoff_ratio=payoff,
    )


def _proportion(realised: int, available: int) -> Decimal | None:
    total = realised + available
    return Decimal(realised) / Decimal(total) if total else None
