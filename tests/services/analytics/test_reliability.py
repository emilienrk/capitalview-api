from decimal import Decimal

from services.analytics.reliability import Metric, Reliability


def _gated(sample_size: int) -> Metric:
    return Metric.gated(
        Decimal("0.14"),
        unit="ratio",
        sample_size=sample_size,
        minimum=10,
        solid_at=30,
        caveat_insufficient="Moins de 10 observations.",
        caveat_indicative="Entre 10 et 30 observations : tendance, pas preuve.",
    )


def test_below_minimum_drops_the_value_entirely():
    metric = _gated(9)
    assert metric.reliability is Reliability.INSUFFICIENT
    assert metric.value is None
    assert metric.caveat == "Moins de 10 observations."


def test_between_thresholds_keeps_value_but_flags_it():
    metric = _gated(15)
    assert metric.reliability is Reliability.INDICATIVE
    assert metric.value == Decimal("0.14")
    assert metric.caveat == "Entre 10 et 30 observations : tendance, pas preuve."


def test_at_solid_threshold_is_solid_without_caveat():
    metric = _gated(30)
    assert metric.reliability is Reliability.SOLID
    assert metric.value == Decimal("0.14")
    assert metric.caveat is None


def test_none_value_is_always_insufficient():
    metric = Metric.gated(
        None,
        unit="ratio",
        sample_size=999,
        minimum=10,
        solid_at=30,
        caveat_insufficient="Non calculable.",
    )
    assert metric.reliability is Reliability.INSUFFICIENT
    assert metric.value is None
