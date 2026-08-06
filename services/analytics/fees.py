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
from enum import Enum

_ZERO = Decimal("0")
_BPS = Decimal("10000")

# Entry cost the threshold is drawn at. 25 bps is the usual line between "a
# rounding error" and "a drag worth restructuring orders for".
TARGET_BPS = Decimal("25")

# Under five orders there is no fee habit to describe, only a couple of numbers.
MIN_ORDERS = 5
SOLID_ORDERS = 20

# Below this share of orders carrying a fee, the recorded ones are too few to
# stand in for the rest and nothing is extrapolated. Above it, the totals are
# estimated over the whole ledger and labelled as estimates — because a fee not
# typed in was still paid, and reporting only what was keyed in understates the
# bill by exactly the share nobody filled.
ESTIMATE_MIN_COVERAGE = Decimal("0.10")

# Enough recorded fees that their average is stable even when it is multiplied
# by a large factor. Coverage guards against extrapolating far; this guards
# against extrapolating from almost nothing, and either one alone has a hole.
ESTIMATE_MIN_SAMPLES = 20

# Three charged orders before the shape of a tariff can be guessed at all.
MIN_MODEL_ORDERS = 3
# Orders all of the same size fit a flat fee and a percentage equally well.
# Below this spread in order size the two models are simply not separable.
MIN_SIZE_SPREAD = Decimal("0.15")
# One model has to fit clearly better than the other, not merely better.
MODEL_MARGIN = Decimal("0.6")


class FeeModel(str, Enum):
    """How the broker appears to charge, read off the orders themselves."""

    FLAT = "fixe"
    PROPORTIONAL = "proportionnel"
    UNKNOWN = "indéterminé"

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
    """The bill for the whole ledger — extrapolated when `is_estimated`."""
    recorded_fees: Decimal
    """What was actually keyed in. Equal to total_fees on a complete ledger."""
    is_estimated: bool
    model: FeeModel
    """How the broker appears to charge. Decides both the estimate and the advice."""
    fee_rate: Decimal | None
    """Fees over notional on the charged orders — what a percentage tariff is."""
    deployed_capital: Decimal
    fee_share: Decimal | None
    """Fees as a share of the capital actually put to work."""
    annual_bps: Decimal | None
    order_count: int
    orders_with_fee: int
    """Orders carrying a recorded fee — the real sample size of every fee figure."""
    average_fee: Decimal | None
    """What the broker takes on a charged order. Free orders are not averaged in."""
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
    def coverage(self) -> Decimal:
        """Share of orders carrying a fee."""
        if self.order_count <= 0:
            return _ZERO
        return Decimal(self.orders_with_fee) / Decimal(self.order_count)

    @property
    def is_too_partial(self) -> bool:
        """Filled too thinly for the recorded orders to stand in for the rest.

        Four fees out of two hundred orders do not describe a broker. Reporting
        their sum as a bill understates it forty-fold, and extrapolating from
        them would be a guess wearing a number's clothes — so neither happens.
        """
        return bool(self.orders_with_fee) and not self.is_estimated and self.coverage < Decimal("1")

    @property
    def grouping_helps(self) -> bool:
        """Whether grouping orders would actually reduce the bill.

        Only under a flat commission. Under a percentage the cost follows the
        euros, not the order count, and "group your orders" is advice that
        changes nothing — worse than silence, because it sounds actionable.
        """
        return self.model is FeeModel.FLAT

    @property
    def is_measurable(self) -> bool:
        # Counted on charged orders, not on orders. Ten imports with no fee
        # column and five real ones describe five fees, whatever the ledger size.
        return self.orders_with_fee >= MIN_ORDERS and not self.is_too_partial


def _cv(values: list[Decimal]) -> Decimal | None:
    """Coefficient of variation — spread relative to the mean, hence unitless.

    That is what lets a spread in euros be compared with a spread in rates.
    """
    if len(values) < 2:
        return None
    mean = sum(values) / Decimal(len(values))
    if mean <= _ZERO:
        return None
    variance = sum((value - mean) ** 2 for value in values) / Decimal(len(values))
    return Decimal(str(float(variance) ** 0.5)) / mean


def detect_fee_model(charged: list[tuple[Decimal, Decimal]]) -> FeeModel:
    """Whether the broker charges per order or per euro, read off the ledger.

    It cannot be assumed. A flat commission makes small orders expensive and
    grouping them the fix; a percentage makes order size irrelevant and grouping
    pure superstition. Advising the second case with the first case's advice is
    how a fee block tells someone to do something that changes nothing.

    Same-size orders fit both models exactly, which is the common case for a
    monthly plan — so that answer is UNKNOWN, not a coin toss.
    """
    if len(charged) < MIN_MODEL_ORDERS:
        return FeeModel.UNKNOWN

    notionals = [notional for notional, _ in charged]
    spread = _cv(notionals)
    if spread is None or spread < MIN_SIZE_SPREAD:
        return FeeModel.UNKNOWN

    flat = _cv([fee for _, fee in charged])
    proportional = _cv([fee / notional for notional, fee in charged])
    if flat is None or proportional is None:
        return FeeModel.UNKNOWN

    if flat <= proportional * MODEL_MARGIN:
        return FeeModel.FLAT
    if proportional <= flat * MODEL_MARGIN:
        return FeeModel.PROPORTIONAL
    return FeeModel.UNKNOWN


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

    recorded_fees = sum(fee for _, fee in orders)
    deployed = sum(notional for notional, _ in orders)
    count = len(orders)
    charged = [(notional, fee) for notional, fee in orders if fee > _ZERO]
    coverage = Decimal(len(charged)) / Decimal(count) if count else _ZERO

    # Averaged over charged orders only. An order with no fee recorded is far
    # more often a fee nobody typed in than a free order, and folding those
    # zeros into the average halves what the broker looks like it charges —
    # which then halves the threshold drawn from it.
    average_fee = recorded_fees / Decimal(len(charged)) if charged else None
    average_order = deployed / Decimal(count)

    model = detect_fee_model(charged)
    charged_notional = sum(notional for notional, _ in charged)
    # What the broker takes per euro put to work, when that is how it charges.
    fee_rate = recorded_fees / charged_notional if charged_notional > _ZERO else None

    # A fee nobody typed in was still paid. Extrapolate it either when the
    # recorded share is large enough that the multiplier stays small, or when
    # there are enough recorded fees for their average to be stable under a
    # large one. Either guard alone leaves a hole: four fees out of five orders
    # is nearly complete data, and fifty fees out of a thousand orders is a
    # solid average — the first fails a sample rule, the second a coverage one.
    is_estimated = (
        bool(charged)
        and coverage < Decimal("1")
        and (coverage >= ESTIMATE_MIN_COVERAGE or len(charged) >= ESTIMATE_MIN_SAMPLES)
    )

    total_fees = recorded_fees
    if is_estimated:
        if model is FeeModel.PROPORTIONAL and fee_rate is not None:
            # Scale by the euros, not by the orders: that is what is charged.
            total_fees = fee_rate * deployed
        elif average_fee:
            total_fees = average_fee * Decimal(count)

    fee_share = total_fees / deployed if deployed > _ZERO else None

    years = _years_of(window)
    annual_bps = None
    if fee_share is not None and years and years > _ZERO:
        annual_bps = fee_share * _BPS / years

    # Below this notional, a flat commission costs more than TARGET_BPS of entry.
    # A percentage commission has no such size: it costs the same rate on a
    # 100 EUR order as on a 10 000 EUR one, so the threshold is not a smaller
    # number here, it is a question that does not apply.
    threshold = (
        average_fee * _BPS / TARGET_BPS
        if average_fee and model is not FeeModel.PROPORTIONAL
        else None
    )

    # Only charged orders can be under it: a free order carries no entry cost to
    # exceed the target with, and counting it would inflate the tally.
    below = [(notional, fee) for notional, fee in charged if threshold and notional < threshold]

    return FeeAnalysis(
        total_fees=total_fees,
        recorded_fees=recorded_fees,
        is_estimated=is_estimated,
        model=model,
        fee_rate=fee_rate,
        deployed_capital=deployed,
        fee_share=fee_share,
        annual_bps=annual_bps,
        order_count=count,
        orders_with_fee=len(charged),
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
