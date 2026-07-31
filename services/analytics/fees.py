"""Brokerage fees, and the only number about them that changes behaviour.

"You paid 210 EUR in fees" is a fact nobody acts on. The threshold is: below a
certain order size, a flat commission eats more than 25 basis points of the money
put to work, and the fix is to group orders rather than to change broker.

One note is not optional and is carried in the output rather than left to the UI:
brokerage is **not** the main cost of a buy-and-hold ETF portfolio. The funds'
ongoing charges (0.15-0.25% a year, typically) are already inside the quoted
price and are **not traceable here**. Reporting order fees without saying so
would leave the reader comfortable about the wrong number.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

_ZERO = Decimal("0")
_BPS = Decimal("10000")

# Entry cost the threshold is drawn at. 25 bps is the usual line between "a
# rounding error" and "a drag worth restructuring orders for".
TARGET_BPS = Decimal("25")

# Under five orders there is no fee habit to describe, only a couple of numbers.
MIN_ORDERS = 5
SOLID_ORDERS = 20

# Projection assumptions, reported alongside the figure — a projection whose
# hypothesis is hidden is an assertion in disguise.
PROJECTION_YEARS = 20
PROJECTION_RATE = Decimal("0.05")

TER_NOTE = (
    "Les frais de courtage ne sont pas ton coût principal. Les frais de gestion des ETF "
    "(TER, typiquement 0,15 à 0,25 % par an) sont déjà dans le cours et ne sont pas traçables "
    "ici : sur un portefeuille buy-and-hold, ils pèsent structurellement plus lourd que tes "
    "frais d'ordre."
)


@dataclass(frozen=True)
class FeeAnalysis:
    total_fees: Decimal
    deployed_capital: Decimal
    fee_share: Decimal | None
    """Fees as a share of the capital actually put to work."""
    annual_bps: Decimal | None
    order_count: int
    average_fee: Decimal | None
    average_order: Decimal | None
    threshold_order_size: Decimal | None
    """Below this order size, entry fees exceed TARGET_BPS."""
    orders_below_threshold: int
    cost_below_threshold: Decimal
    invested_below_threshold: Decimal
    projection_eur: Decimal | None
    """What today's fee cadence compounds to over PROJECTION_YEARS."""
    ter_note: str = TER_NOTE

    @property
    def is_measurable(self) -> bool:
        return self.order_count >= MIN_ORDERS

    @property
    def is_avoidable(self) -> bool:
        """Whether grouping orders is worth recommending at all.

        The per-order threshold and the annual charge answer different questions,
        and reading the first as advice while the second is already low produces a
        contradiction: at 0.68 EUR an order every order sits under the 272 EUR
        threshold, yet the whole fee load is 20 bps a year. Telling someone to
        group orders there is telling them to break a DCA habit to save a few
        euros. The threshold stays on screen as calibration; only the annual
        charge decides whether there is anything to do.
        """
        if self.annual_bps is None or self.orders_below_threshold == 0:
            return False
        return self.annual_bps > TARGET_BPS


def _tx_type(tx) -> str:
    raw = getattr(tx, "type", None)
    return str(getattr(raw, "value", raw) or "")


def _dec(value) -> Decimal:
    if value is None:
        return _ZERO
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:
        return _ZERO


def analyse_fees(transactions, window) -> FeeAnalysis | None:
    """Fees paid on purchases, and the order size that makes them stop mattering."""
    orders: list[tuple[Decimal, Decimal]] = []
    for tx in transactions or ():
        if _tx_type(tx) != "BUY":
            continue
        if str(getattr(tx, "asset_key", "") or "").upper() == "EUR":
            continue
        notional = _dec(getattr(tx, "amount", None)) * _dec(getattr(tx, "price_per_unit", None))
        if notional <= _ZERO:
            continue
        orders.append((notional, _dec(getattr(tx, "fees", None))))

    if not orders:
        return None

    total_fees = sum(fee for _, fee in orders)
    deployed = sum(notional for notional, _ in orders)
    count = len(orders)

    average_fee = total_fees / Decimal(count)
    average_order = deployed / Decimal(count)
    fee_share = total_fees / deployed if deployed > _ZERO else None

    years = _years_of(window)
    annual_bps = None
    if fee_share is not None and years and years > _ZERO:
        annual_bps = fee_share * _BPS / years

    # Below this notional, a flat commission costs more than TARGET_BPS of entry.
    threshold = average_fee * _BPS / TARGET_BPS if average_fee > _ZERO else None

    below = [(notional, fee) for notional, fee in orders if threshold and notional < threshold]

    return FeeAnalysis(
        total_fees=total_fees,
        deployed_capital=deployed,
        fee_share=fee_share,
        annual_bps=annual_bps,
        order_count=count,
        average_fee=average_fee,
        average_order=average_order,
        threshold_order_size=threshold,
        orders_below_threshold=len(below),
        cost_below_threshold=sum(fee for _, fee in below),
        invested_below_threshold=sum(notional for notional, _ in below),
        projection_eur=_project(total_fees, years),
    )


def _years_of(window) -> Decimal | None:
    if window is None or window.start is None or window.end is None:
        return None
    days = (window.end - window.start).days
    return Decimal(days) / Decimal("365") if days > 0 else None


def _project(total_fees: Decimal, years: Decimal | None) -> Decimal | None:
    """What the current fee cadence compounds to over twenty years.

    Assumption, stated in the output: the same yearly fee spend, each year's fees
    compounding at PROJECTION_RATE until the horizon. It is the opportunity cost
    of the fees, not the fees themselves.
    """
    if not years or years <= _ZERO or total_fees <= _ZERO:
        return None
    yearly = total_fees / years
    rate = float(PROJECTION_RATE)
    total = sum(float(yearly) * (1.0 + rate) ** (PROJECTION_YEARS - n) for n in range(PROJECTION_YEARS))
    return Decimal(str(round(total, 2)))


def monthly_fee_series(transactions) -> list[tuple[date, Decimal, Decimal]]:
    """(day, notional, fee) per purchase — the scatter behind the threshold."""
    out: list[tuple[date, Decimal, Decimal]] = []
    for tx in transactions or ():
        if _tx_type(tx) != "BUY":
            continue
        executed_at = getattr(tx, "executed_at", None)
        if executed_at is None:
            continue
        notional = _dec(getattr(tx, "amount", None)) * _dec(getattr(tx, "price_per_unit", None))
        if notional > _ZERO:
            out.append((executed_at.date(), notional, _dec(getattr(tx, "fees", None))))
    return sorted(out)
