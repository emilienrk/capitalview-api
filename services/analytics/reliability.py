"""Reliability gating for investor analytics.

Two years of retail data is a small sample. The risk is not computing a number
wrongly, it is displaying a correct number with false confidence. Every metric
therefore carries how far it can be trusted, and a metric below its minimum
sample has its value dropped here rather than in the UI — an unreliable figure
that never reaches the client cannot be misread.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class Reliability(str, Enum):
    SOLID = "solide"
    INDICATIVE = "indicatif"
    ESTIMATED = "estimé"
    """Extrapolated from a partial ledger — a figure, but not one that was read."""
    INSUFFICIENT = "insuffisant"


@dataclass(frozen=True)
class Metric:
    value: Decimal | None
    unit: str
    sample_size: int
    reliability: Reliability
    caveat: str | None = None

    @classmethod
    def gated(
        cls,
        value: Decimal | None,
        *,
        unit: str,
        sample_size: int,
        minimum: int,
        solid_at: int,
        caveat_insufficient: str,
        caveat_indicative: str | None = None,
        estimated: bool = False,
        caveat_estimated: str | None = None,
    ) -> "Metric":
        if value is None or sample_size < minimum:
            return cls(None, unit, sample_size, Reliability.INSUFFICIENT, caveat_insufficient)
        # Extrapolated outranks merely thin: that the number was never read is
        # the more important thing to say about it, and both cannot be shown.
        if estimated:
            return cls(value, unit, sample_size, Reliability.ESTIMATED, caveat_estimated)
        if sample_size < solid_at:
            return cls(value, unit, sample_size, Reliability.INDICATIVE, caveat_indicative)
        return cls(value, unit, sample_size, Reliability.SOLID, None)
