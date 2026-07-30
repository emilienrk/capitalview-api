"""Permutation testing — the shared engine.

A behavioural claim needs a null hypothesis, otherwise "you buy after the monthly
rise" is indistinguishable from forty-seven coin flips that happened to land the
same way. Every M2/M3 block that says something affirmative about behaviour goes
through here first.

The engine is deliberately ignorant of what it is testing: it takes an observed
statistic and an array of statistics drawn under the null, and reports where the
observation falls. Blocks own the resampling scheme, because only they know what
"holding everything else constant" means for their question.
"""

from dataclasses import dataclass

import numpy as np

# A fixed seed is a correctness requirement, not laziness: without it the same
# portfolio yields a different p-value on every page load, and a number that
# moves when nothing changed is a number nobody should trust.
DEFAULT_SEED = 0

DEFAULT_DRAWS = 5000

# Above this the effect is indistinguishable from chance and the UI must say
# "nothing detectable" rather than crediting or blaming the user (spec section 2).
DETECTABLE_P = 0.10


@dataclass(frozen=True)
class PermutationResult:
    observed: float
    p_value: float
    percentile: float
    n_draws: int

    @property
    def is_detectable(self) -> bool:
        return self.p_value <= DETECTABLE_P


def rng(seed: int = DEFAULT_SEED) -> np.random.Generator:
    """The generator every block should use, so results stay reproducible."""
    return np.random.default_rng(seed)


def permutation_test(observed: float, null_samples) -> PermutationResult | None:
    """Locate an observed statistic inside its null distribution.

    Two-sided by construction: a systematically favourable execution is as much a
    finding as an unfavourable one, and deciding the direction after seeing the
    data is how false positives are manufactured.

    The p-value uses the (r+1)/(n+1) convention rather than r/n, so a statistic no
    draw reaches reports 1/(n+1) instead of an impossible zero.
    """
    samples = np.asarray(null_samples, dtype=np.float64)
    samples = samples[np.isfinite(samples)]
    if samples.size == 0 or not np.isfinite(observed):
        return None

    centre = float(np.mean(samples))
    at_least_as_extreme = int(np.sum(np.abs(samples - centre) >= abs(observed - centre)))
    p_value = (at_least_as_extreme + 1) / (samples.size + 1)
    percentile = float(np.mean(samples < observed) * 100.0)

    return PermutationResult(
        observed=float(observed),
        p_value=min(p_value, 1.0),
        percentile=percentile,
        n_draws=int(samples.size),
    )
