"""Where each headline figure sits on the scale the page judges it by.

The bands live here and nowhere else. The page draws them, the signals read
them, and a scale that disagreed with the figures under it would be worse than
no scale — which is what happened while the front kept its own copy.

A band runs up to and including `up_to`; the last one has none and runs to
infinity.
"""

from dataclasses import dataclass
from decimal import Decimal

from services.analytics.fees import TARGET_BPS

GOOD = "good"
WATCH = "watch"
BAD = "bad"
NEUTRAL = "neutral"

_ZERO = Decimal("0")


@dataclass(frozen=True)
class Band:
    up_to: Decimal | None
    label: str
    tone: str


def regularity_bands(purchase_count: int) -> list[Band]:
    # Discrete orders leave a floor of about 1/(2n) even on a perfect rhythm, so
    # the line is held up to twice that floor.
    straight = Decimal("1") / Decimal(max(purchase_count, 1))
    return [
        Band(min(straight, Decimal("0.25")), "régulier", GOOD),
        Band(Decimal("0.25"), "à-coups", WATCH),
        Band(None, "gros à-coups", BAD),
    ]


DEPOSIT_LAG_BANDS = [
    Band(Decimal("2"), "investi à l'arrivée", GOOD),
    Band(Decimal("7"), "quelques jours", WATCH),
    Band(None, "le cash attend", BAD),
]

INDEPENDENT_BETS_BANDS = [
    Band(Decimal("1.5"), "un seul pari", BAD),
    Band(Decimal("2.5"), "deux directions", WATCH),
    Band(None, "réparti", GOOD),
]

FEES_BANDS = [
    Band(TARGET_BPS, "léger", GOOD),
    Band(Decimal("75"), "visible", WATCH),
    Band(None, "lourd", BAD),
]

DISPOSITION_BANDS = [
    Band(Decimal("1"), "neutre", GOOD),
    Band(Decimal("2"), "gains vendus tôt", WATCH),
    Band(None, "très marqué", BAD),
]

ADHERENCE_BANDS = [
    Band(Decimal("0.8"), "décroché", BAD),
    Band(Decimal("0.98"), "en retrait", WATCH),
    Band(None, "tenu", GOOD),
]


def reading(value, bands: list[Band], fmt: str) -> dict:
    """The figure, the band it falls in, and every band for the scale.

    `fmt` tells the client how to print the value and the ceilings: pct (a
    ratio), days, decimal, bps or times.
    """
    active = None
    if value is not None:
        for index, band in enumerate(bands):
            if band.up_to is None or value <= band.up_to:
                active = index
                break
    return {
        "value": value,
        "format": fmt,
        "active": active,
        "tone": bands[active].tone if active is not None else None,
        "bands": [{"up_to": band.up_to, "label": band.label, "tone": band.tone} for band in bands],
    }


_TONE_RANK = {BAD: 0, WATCH: 1, GOOD: 2, NEUTRAL: 3}


def build_signals(blocks: dict) -> list[dict]:
    """One line per block that passed its gate: what is off shows first.

    Like the global verdict, it reads gated payloads only — a withheld block
    contributes no line at all. Euros are signed from the reader's side: negative
    is money lost.
    """
    signals: list[dict] = []

    def add(block, label, tone, *, value=None, fmt=None, eur=None):
        signals.append(
            {
                "block": block,
                "label": label,
                "tone": tone,
                "value": value,
                "format": fmt,
                "eur": round(eur) if eur is not None else None,
            }
        )

    def from_reading(block, label, payload, *, eur=None):
        rd = payload.get("reading") if payload else None
        if rd and rd["tone"] is not None:
            add(block, label, rd["tone"], value=rd["value"], fmt=rd["format"], eur=eur)

    bridge = blocks.get("counterfactual")
    if bridge:
        cost = bridge["behaviour_cost"]
        add("counterfactual", "Face au robot indexé", BAD if cost < _ZERO else GOOD, eur=cost)
        if bridge["idle_cash_opportunity"] and bridge["idle_cash"] > _ZERO:
            add(
                "counterfactual",
                "Cash non investi",
                BAD,
                eur=-abs(bridge["idle_cash_opportunity"]),
            )

    gap = blocks.get("investor_gap")
    if gap and gap["gap_eur"]["value"] is not None:
        amount = gap["gap_eur"]["value"]
        add("investor_gap", "Moment des versements", BAD if amount < _ZERO else GOOD, eur=amount)

    execution = blocks.get("execution")
    if execution and execution["cost_eur"]["value"] is not None:
        cost = execution["cost_eur"]["value"]
        if not execution["is_detectable"]:
            add("execution", "Prix d'achat", NEUTRAL, value=execution["slippage_bps"]["value"], fmt="bps")
        else:
            add("execution", "Prix d'achat", BAD if cost > _ZERO else GOOD, eur=-cost)

    from_reading("regularity", "Irrégularité", blocks.get("regularity"))
    from_reading("deposit_lag", "Délai virement → achat", blocks.get("deposit_lag"))

    conditioning = blocks.get("market_conditioning")
    if conditioning and conditioning["weighted_drawdown"]["value"] is not None:
        mine = conditioning["weighted_drawdown"]["value"]
        average = conditioning["unconditional_drawdown"]["value"]
        tone = NEUTRAL
        if conditioning["is_detectable"] and average is not None:
            # Drawdowns are negative: a larger one means buying further below the high.
            tone = GOOD if mine < average else BAD
        add("market_conditioning", "Moment dans le marché", tone, value=mine, fmt="pct")

    from_reading("concentration", "Paris indépendants", blocks.get("concentration"))
    # The annual load, not the euros paid: every portfolio pays fees, and a green
    # line reading "−59 €" says the opposite of what it means.
    from_reading("fees", "Frais de courtage", blocks.get("fees"))

    exits = blocks.get("exits")
    exit_cost = exits["cost_eur"]["value"] if exits else None
    from_reading("exits", "Ventes", exits, eur=-exit_cost if exit_cost is not None else None)

    plan = blocks.get("plan")
    if plan and not plan.get("error"):
        shortfall = plan["total_target"] - plan["total_invested"]
        from_reading("plan", "Plan cible", plan, eur=-shortfall if shortfall > _ZERO else None)

    signals.sort(key=lambda s: (_TONE_RANK[s["tone"]], -abs(s["eur"] or _ZERO)))
    return signals
