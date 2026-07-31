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
        caveat_uncomputable: str | None = None,
    ) -> "Metric":
        # Two different reasons produce the same "insuffisant", and telling a
        # reader "936 days of history: too short to conclude" about a number that
        # simply could not be computed reads as nonsense. When the sample clears
        # the bar, say the number is missing, not that the history is.
        if value is None and sample_size >= minimum:
            return cls(
                None,
                unit,
                sample_size,
                Reliability.INSUFFICIENT,
                caveat_uncomputable or "Cette valeur n'a pas pu être calculée sur tes données.",
            )
        if value is None or sample_size < minimum:
            return cls(None, unit, sample_size, Reliability.INSUFFICIENT, caveat_insufficient)
        if sample_size < solid_at:
            return cls(value, unit, sample_size, Reliability.INDICATIVE, caveat_indicative)
        return cls(value, unit, sample_size, Reliability.SOLID, None)
