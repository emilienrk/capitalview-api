"""Fund exposure reference data: the hand-kept file, and the reads over it.

The file (data/fund_exposure.toml) is the only writer today. It is synced into
`fund_exposures` at startup by difference, never by delete-and-reinsert: the
analytics cache fingerprints this table, and a restart that rewrote every row
would throw away every user's cached report for nothing.

A synthetic ETF is exposed to its index, not to what it holds — its holdings
file lists the substitute basket behind the swap. So the weights in the file
are the index's, and nothing here ever derives an exposure from holdings.
"""

import logging
import re
import tomllib
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

import sqlalchemy as sa
from sqlmodel import Session, select

from models.fund_exposure import FundExposure

logger = logging.getLogger(__name__)

ORIGIN_MANUAL = "manual"
KIND_SECTOR = "sector"
KIND_COUNTRY = "country"
KIND_TER = "ter"

SECTORS = (
    "technology",
    "communication",
    "health_care",
    "financials",
    "real_estate",
    "consumer_discretionary",
    "consumer_staples",
    "industrials",
    "materials",
    "energy",
    "utilities",
)

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "fund_exposure.toml"

# Factsheets round each weight, so a complete breakdown can overshoot 100 % by
# a few hundredths of a point.
_SUM_TOLERANCE = Decimal("0.005")
_MAX_TER = Decimal("0.05")
_COUNTRY = re.compile(r"^[A-Z]{2}$")
_ZERO = Decimal("0")
_ONE = Decimal("1")


class ExposureFileError(ValueError):
    pass


@dataclass(frozen=True)
class ExposureRow:
    asset_key: str
    kind: str
    label: str
    weight: Decimal
    as_of: date
    source: str


@dataclass
class FundProfile:
    sectors: dict[str, Decimal] = field(default_factory=dict)
    sectors_as_of: date | None = None
    countries: dict[str, Decimal] = field(default_factory=dict)
    countries_as_of: date | None = None
    ter: Decimal | None = None
    ter_as_of: date | None = None


def _dated(block: dict, where: str) -> tuple[date, str]:
    as_of, source = block.get("as_of"), block.get("source")
    if not isinstance(as_of, date):
        raise ExposureFileError(f"{where}: `as_of` must be a date (2026-10-30)")
    if not isinstance(source, str) or not source.strip():
        raise ExposureFileError(f"{where}: `source` is required")
    return as_of, source.strip()


def _weights(block: dict, kind: str, where: str) -> dict[str, Decimal]:
    raw = block.get("weights")
    if not isinstance(raw, dict) or not raw:
        raise ExposureFileError(f"{where}: `weights` must be a non-empty table")
    weights: dict[str, Decimal] = {}
    for label, weight in raw.items():
        if kind == KIND_COUNTRY and not _COUNTRY.match(label):
            raise ExposureFileError(f"{where}: `{label}` is not an ISO 3166 alpha-2 code")
        if kind == KIND_SECTOR and label not in SECTORS:
            raise ExposureFileError(f"{where}: unknown sector `{label}`")
        if not isinstance(weight, Decimal) or not _ZERO < weight <= _ONE:
            raise ExposureFileError(f"{where}: `{label}` must be a ratio in ]0, 1]")
        weights[label] = weight
    if sum(weights.values()) > _ONE + _SUM_TOLERANCE:
        raise ExposureFileError(f"{where}: weights sum to more than 1")
    return weights


def parse_exposure_file(text: str) -> list[ExposureRow]:
    """Every row the file describes. Raises ExposureFileError on anything off."""
    try:
        data = tomllib.loads(text, parse_float=Decimal)
    except tomllib.TOMLDecodeError as error:
        raise ExposureFileError(str(error)) from error

    rows: list[ExposureRow] = []
    for isin, fund in data.items():
        if not isinstance(fund, dict):
            raise ExposureFileError(f"{isin}: expected a table")
        key = isin.strip().upper()

        ter = fund.get("ter")
        if ter is not None:
            as_of, source = _dated(ter, f"{key}.ter")
            value = ter.get("value")
            if not isinstance(value, Decimal) or not _ZERO <= value <= _MAX_TER:
                raise ExposureFileError(f"{key}.ter: `value` must be a ratio up to {_MAX_TER}")
            rows.append(ExposureRow(key, KIND_TER, "", value, as_of, source))

        for kind, name in ((KIND_COUNTRY, "countries"), (KIND_SECTOR, "sectors")):
            block = fund.get(name)
            if block is None:
                continue
            where = f"{key}.{name}"
            as_of, source = _dated(block, where)
            for label, weight in _weights(block, kind, where).items():
                rows.append(ExposureRow(key, kind, label, weight, as_of, source))
    return rows


def sync_exposure_rows(session: Session, rows: list[ExposureRow], origin: str = ORIGIN_MANUAL) -> dict:
    """Make the rows of `origin` match `rows`, touching only what differs."""
    wanted = {(row.asset_key, row.kind, row.label): row for row in rows}
    existing = {
        (row.asset_key, row.kind, row.label): row
        for row in session.exec(select(FundExposure).where(FundExposure.origin == origin)).all()
    }

    counters = {"inserted": 0, "updated": 0, "deleted": 0}
    for key, stored in existing.items():
        if key not in wanted:
            session.delete(stored)
            counters["deleted"] += 1
    for key, row in wanted.items():
        stored = existing.get(key)
        if stored is None:
            session.add(
                FundExposure(
                    asset_key=row.asset_key,
                    origin=origin,
                    kind=row.kind,
                    label=row.label,
                    weight=row.weight,
                    as_of=row.as_of,
                    source=row.source,
                )
            )
            counters["inserted"] += 1
        elif (Decimal(stored.weight), stored.as_of, stored.source) != (row.weight, row.as_of, row.source):
            stored.weight, stored.as_of, stored.source = row.weight, row.as_of, row.source
            session.add(stored)
            counters["updated"] += 1
    session.commit()
    return counters


def sync_exposure_file(path: Path = DEFAULT_PATH) -> dict:
    """Startup job: load the hand-kept file into the table."""
    from database import get_engine

    rows = parse_exposure_file(path.read_text(encoding="utf-8"))
    with Session(get_engine()) as session:
        counters = sync_exposure_rows(session, rows)
    logger.info("fund exposure file synced", extra={"counters": counters})
    return counters


def load_profiles(session: Session, asset_keys) -> dict[str, FundProfile]:
    """The exposure of each asset asked, for the assets that have any.

    Per asset and kind, the hand-kept rows win whole: a file that lists five
    countries is not topped up with a provider's sixth, which would add up two
    sources dated differently.
    """
    keys = [key for key in dict.fromkeys(asset_keys or ()) if key]
    if not keys:
        return {}
    rows = session.exec(select(FundExposure).where(FundExposure.asset_key.in_(keys))).all()

    chosen: dict[tuple[str, str], str] = {}
    for row in rows:
        slot = (row.asset_key, row.kind)
        current = chosen.get(slot)
        if current is None or row.origin == ORIGIN_MANUAL or (current != ORIGIN_MANUAL and row.origin < current):
            chosen[slot] = row.origin

    profiles: dict[str, FundProfile] = {}
    for row in rows:
        if chosen[(row.asset_key, row.kind)] != row.origin:
            continue
        profile = profiles.setdefault(row.asset_key, FundProfile())
        weight = Decimal(row.weight)
        if row.kind == KIND_SECTOR:
            profile.sectors[row.label] = weight
            profile.sectors_as_of = row.as_of
        elif row.kind == KIND_COUNTRY:
            profile.countries[row.label] = weight
            profile.countries_as_of = row.as_of
        elif row.kind == KIND_TER:
            profile.ter = weight
            profile.ter_as_of = row.as_of
    return profiles


def exposure_signature(session: Session) -> list:
    """Count and latest write of the table, for the analytics cache digest."""
    return list(
        session.exec(select(sa.func.count(), sa.func.max(FundExposure.updated_at))).one()
    )
