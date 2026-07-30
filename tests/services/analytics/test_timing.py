import numpy as np

from services.analytics.timing import (
    DEFAULT_DRAWS,
    permutation_test,
    rng,
)


def test_an_observation_in_the_middle_of_the_null_is_not_detectable():
    samples = rng().normal(0, 1, 5000)

    result = permutation_test(0.0, samples)

    assert result.p_value > 0.5
    assert result.is_detectable is False


def test_an_observation_far_outside_the_null_is_detectable():
    samples = rng().normal(0, 1, 5000)

    result = permutation_test(6.0, samples)

    assert result.p_value < 0.01
    assert result.is_detectable is True
    assert result.percentile > 99


def test_p_value_is_never_zero():
    """An unreachable statistic reports 1/(n+1), not an impossible certainty."""
    result = permutation_test(1e6, rng().normal(0, 1, 100))

    assert result.p_value == 1 / 101


def test_the_test_is_two_sided():
    samples = rng().normal(0, 1, 5000)

    assert permutation_test(4.0, samples).p_value == permutation_test(-4.0, samples).p_value


def test_empty_or_non_finite_input_yields_no_result():
    assert permutation_test(1.0, []) is None
    assert permutation_test(float("nan"), [1.0, 2.0]) is None
    assert permutation_test(1.0, [float("nan"), float("inf")]) is None


def test_the_seed_makes_results_reproducible():
    assert rng().normal(0, 1, 10).tolist() == rng().normal(0, 1, 10).tolist()
    assert rng(1).normal(0, 1, 10).tolist() != rng(2).normal(0, 1, 10).tolist()


def test_p_values_are_uniform_on_data_with_no_bias():
    """Calibration check required by spec section 11.

    On synthetic data where the null is true, p-values must be roughly uniform on
    [0,1]. A test that reports significance more often than chance would turn
    noise into behavioural accusations.
    """
    generator = rng(12345)
    p_values = []
    for _ in range(200):
        draws = generator.normal(0, 1, 400)
        # Observed statistic drawn from the same distribution: the null holds.
        observed = float(generator.normal(0, 1))
        p_values.append(permutation_test(observed, draws).p_value)

    p_values = np.asarray(p_values)
    below_5pct = float(np.mean(p_values < 0.05))
    below_50pct = float(np.mean(p_values < 0.50))

    # Wide bounds on purpose: this must catch a broken test, not flap on sampling.
    assert 0.0 <= below_5pct <= 0.15
    assert 0.35 <= below_50pct <= 0.65


def test_default_draw_count_matches_the_spec():
    assert DEFAULT_DRAWS == 5000
