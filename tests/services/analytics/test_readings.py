from decimal import Decimal

from services.analytics.readings import (
    DEPOSIT_LAG_BANDS,
    FEES_BANDS,
    build_signals,
    reading,
    regularity_bands,
)


def _metric(value):
    return {"value": value, "unit": "x", "sample_size": 1, "reliability": "solide", "caveat": None}


def test_a_ceiling_belongs_to_its_own_band():
    # 25 bps is the target itself: on it, the load is still under the line.
    assert reading(Decimal("25"), FEES_BANDS, "bps")["tone"] == "good"
    assert reading(Decimal("25.01"), FEES_BANDS, "bps")["tone"] == "watch"
    assert reading(Decimal("400"), FEES_BANDS, "bps")["active"] == 2


def test_a_withheld_value_is_placed_nowhere():
    result = reading(None, DEPOSIT_LAG_BANDS, "days")

    assert result["active"] is None and result["tone"] is None
    # The scale itself is still sent: the block can draw it empty.
    assert len(result["bands"]) == 3
    assert result["bands"][-1]["up_to"] is None


def test_the_regular_band_follows_the_floor_of_discrete_orders():
    # 40 orders leave a floor near 1/80; the line holds up to twice that.
    assert regularity_bands(40)[0].up_to == Decimal("0.025")
    # Too few orders never push "regular" past the next band.
    assert regularity_bands(2)[0].up_to == Decimal("0.25")


def test_signals_put_what_is_off_first_and_rank_it_by_euros():
    blocks = {
        "counterfactual": {
            "behaviour_cost": Decimal("-300"),
            "idle_cash": Decimal("5000"),
            "idle_cash_opportunity": Decimal("900"),
        },
        "deposit_lag": {"reading": reading(Decimal("1"), DEPOSIT_LAG_BANDS, "days")},
        "fees": {
            "total_fees": _metric(Decimal("40")),
            "reading": reading(Decimal("90"), FEES_BANDS, "bps"),
        },
    }

    signals = build_signals(blocks)

    assert [s["label"] for s in signals] == [
        "Cash non investi",
        "Face au robot indexé",
        "Frais de courtage",
        "Délai virement → achat",
    ]
    assert signals[0]["eur"] == Decimal("-900")


def test_a_withheld_block_sends_no_signal():
    blocks = {"deposit_lag": {"reading": reading(None, DEPOSIT_LAG_BANDS, "days")}}

    assert build_signals(blocks) == []
