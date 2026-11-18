"""The hand-kept fund file, and how it reaches the table."""

from datetime import date
from decimal import Decimal

import pytest
from sqlmodel import select

from models.fund_exposure import FundExposure
from services.fund_exposure import (
    DEFAULT_PATH,
    ExposureFileError,
    exposure_signature,
    load_profiles,
    parse_exposure_file,
    sync_exposure_rows,
)

FUND = """
[FR0000000001]
name = "A fund"

[FR0000000001.ter]
value = 0.0020
as_of = 2026-11-01
source = "https://example.com/kid"

[FR0000000001.countries]
as_of = 2026-10-30
source = "https://example.com/factsheet"
weights = { US = 0.70, JP = 0.05 }
"""


def test_the_shipped_file_parses():
    rows = parse_exposure_file(DEFAULT_PATH.read_text(encoding="utf-8"))

    assert {row.asset_key for row in rows} >= {"FR0011550185", "FR0011871110"}


def test_a_fund_reads_into_one_row_per_weight_and_one_for_its_fee():
    rows = parse_exposure_file(FUND)

    assert [(row.kind, row.label, row.weight) for row in rows] == [
        ("ter", "", Decimal("0.0020")),
        ("country", "US", Decimal("0.70")),
        ("country", "JP", Decimal("0.05")),
    ]
    assert rows[1].as_of == date(2026, 10, 30)


@pytest.mark.parametrize(
    "bad",
    [
        FUND.replace("US = 0.70", "USA = 0.70"),
        FUND.replace("US = 0.70", "US = 0.99"),
        FUND.replace('source = "https://example.com/factsheet"\n', ""),
        FUND.replace("as_of = 2026-10-30", 'as_of = "October"'),
        FUND.replace("value = 0.0020", "value = 0.20"),
        FUND + '\n[FR0000000001.sectors]\nas_of = 2026-10-30\nsource = "x"\nweights = { tech = 1.0 }\n',
    ],
    ids=["country-code", "over-one", "no-source", "no-date", "ter-as-percent", "unknown-sector"],
)
def test_a_malformed_fund_is_refused_rather_than_half_loaded(bad):
    with pytest.raises(ExposureFileError):
        parse_exposure_file(bad)


def test_rounded_factsheet_weights_may_overshoot_one_by_a_hair():
    rows = parse_exposure_file(FUND.replace("JP = 0.05", "JP = 0.3001"))

    assert sum(row.weight for row in rows if row.kind == "country") == Decimal("1.0001")


def test_a_resync_of_an_unchanged_file_touches_nothing(session):
    rows = parse_exposure_file(FUND)
    sync_exposure_rows(session, rows)
    signature = exposure_signature(session)

    counters = sync_exposure_rows(session, rows)

    assert counters == {"inserted": 0, "updated": 0, "deleted": 0}
    assert exposure_signature(session) == signature


def test_a_weight_removed_from_the_file_leaves_the_table(session):
    sync_exposure_rows(session, parse_exposure_file(FUND))

    counters = sync_exposure_rows(session, parse_exposure_file(FUND.replace(", JP = 0.05", "")))

    assert counters["deleted"] == 1
    labels = session.exec(select(FundExposure.label).where(FundExposure.kind == "country")).all()
    assert labels == ["US"]


def test_the_hand_kept_rows_win_whole_over_another_source(session):
    sync_exposure_rows(session, parse_exposure_file(FUND))
    provider = parse_exposure_file(
        FUND.replace("US = 0.70, JP = 0.05", "US = 0.60, GB = 0.10").replace("value = 0.0020", "value = 0.0030")
    )
    sync_exposure_rows(session, provider, origin="provider")

    profile = load_profiles(session, ["FR0000000001"])["FR0000000001"]

    assert profile.countries == {"US": Decimal("0.70"), "JP": Decimal("0.05")}
    assert profile.ter == Decimal("0.0020")


def test_another_source_fills_a_kind_the_file_leaves_out(session):
    sync_exposure_rows(session, parse_exposure_file(FUND))
    sectors = parse_exposure_file(
        '[FR0000000001.sectors]\nas_of = 2026-10-30\nsource = "x"\nweights = { technology = 0.4 }\n'
    )
    sync_exposure_rows(session, sectors, origin="provider")

    profile = load_profiles(session, ["FR0000000001"])["FR0000000001"]

    assert profile.sectors == {"technology": Decimal("0.4")}
    assert profile.countries["US"] == Decimal("0.70")
