"""What the portfolio is exposed to: sectors, countries, and the funds' own fees.

The portfolio weight of each line, multiplied by the exposure of its fund. A
line with no known exposure is not spread over the others: it is counted as
unknown, so the page can say how much of the portfolio the breakdown covers
instead of presenting a partial picture as the whole.

Likewise the part of a fund its source does not detail ("Other" on a factsheet)
stays "other" — it is not redistributed over the listed countries.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from services.fund_exposure import FundProfile

_ZERO = Decimal("0")
_ONE = Decimal("1")


@dataclass(frozen=True)
class Breakdown:
    shares: list[tuple[str, Decimal]]
    """Share of the whole portfolio per label, largest first."""
    other: Decimal
    """Covered lines, but the part their source does not detail."""
    unknown: Decimal
    """Lines with no breakdown at all."""
    oldest_as_of: date | None

    @property
    def coverage(self) -> Decimal:
        return _ONE - self.unknown


@dataclass(frozen=True)
class LineExposure:
    asset_key: str
    weight: Decimal
    ter: Decimal | None
    sectors_as_of: date | None
    countries_as_of: date | None


@dataclass(frozen=True)
class Exposure:
    sectors: Breakdown
    countries: Breakdown
    annual_fund_fees: Decimal | None
    """Euros a year at today's values, over the lines whose fee is known."""
    weighted_ter: Decimal | None
    fee_coverage: Decimal
    lines: list[LineExposure]


def _breakdown(weights, profiles: dict[str, FundProfile], kind: str) -> Breakdown:
    shares: dict[str, Decimal] = {}
    other = unknown = _ZERO
    dates: list[date] = []
    for key, weight in weights:
        profile = profiles.get(key)
        distribution = getattr(profile, kind) if profile else None
        if not distribution:
            unknown += weight
            continue
        for label, share in distribution.items():
            shares[label] = shares.get(label, _ZERO) + weight * share
        other += weight * max(_ONE - sum(distribution.values()), _ZERO)
        as_of = getattr(profile, f"{kind}_as_of")
        if as_of:
            dates.append(as_of)
    return Breakdown(
        shares=sorted(shares.items(), key=lambda pair: (-pair[1], pair[0])),
        other=other,
        unknown=unknown,
        oldest_as_of=min(dates) if dates else None,
    )


def fund_breakdowns(asset_key: str, profile: FundProfile) -> tuple[Breakdown, Breakdown]:
    """Sectors and countries of a single fund, as if it were the whole portfolio."""
    weights = [(asset_key, _ONE)]
    profiles = {asset_key: profile}
    return _breakdown(weights, profiles, "sectors"), _breakdown(weights, profiles, "countries")


def analyse_exposure(weights, portfolio_value: Decimal, profiles: dict[str, FundProfile]) -> Exposure | None:
    """`weights` as concentration.portfolio_weights returns them: value weights summing to one."""
    if not weights:
        return None

    fees = _ZERO
    fee_coverage = _ZERO
    lines: list[LineExposure] = []
    for key, weight in weights:
        profile = profiles.get(key)
        ter = profile.ter if profile else None
        if ter is not None:
            fees += weight * portfolio_value * ter
            fee_coverage += weight
        lines.append(
            LineExposure(
                asset_key=key,
                weight=weight,
                ter=ter,
                sectors_as_of=profile.sectors_as_of if profile and profile.sectors else None,
                countries_as_of=profile.countries_as_of if profile and profile.countries else None,
            )
        )

    covered_value = fee_coverage * portfolio_value
    return Exposure(
        sectors=_breakdown(weights, profiles, "sectors"),
        countries=_breakdown(weights, profiles, "countries"),
        annual_fund_fees=fees if fee_coverage > _ZERO else None,
        weighted_ter=fees / covered_value if covered_value > _ZERO else None,
        fee_coverage=fee_coverage,
        lines=lines,
    )
