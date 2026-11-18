"""Sectors, countries and the funds' fees, weighted by the portfolio."""

from datetime import date
from decimal import Decimal

from services.analytics.exposure import analyse_exposure
from services.fund_exposure import FundProfile

D = Decimal


def _us_fund():
    return FundProfile(
        countries={"US": D("0.95"), "GB": D("0.02")},
        countries_as_of=date(2026, 9, 30),
        sectors={"technology": D("0.6"), "communication": D("0.4")},
        sectors_as_of=date(2026, 10, 30),
        ter=D("0.003"),
    )


def _europe_fund():
    return FundProfile(
        countries={"GB": D("0.25"), "FR": D("0.15")},
        countries_as_of=date(2026, 10, 30),
        ter=D("0.002"),
    )


def test_each_line_weighs_its_fund_by_its_share_of_the_portfolio():
    exposure = analyse_exposure(
        [("US1", D("0.6")), ("EU1", D("0.4"))], D("10000"), {"US1": _us_fund(), "EU1": _europe_fund()}
    )

    countries = dict(exposure.countries.shares)
    assert countries["US"] == D("0.570")
    assert countries["GB"] == D("0.012") + D("0.100")
    assert countries["FR"] == D("0.060")


def test_what_a_factsheet_leaves_undetailed_stays_other_instead_of_being_spread():
    exposure = analyse_exposure(
        [("US1", D("0.6")), ("EU1", D("0.4"))], D("10000"), {"US1": _us_fund(), "EU1": _europe_fund()}
    )

    # 3 % of the US fund and 60 % of the European one are not detailed.
    assert exposure.countries.other == D("0.6") * D("0.03") + D("0.4") * D("0.60")
    assert exposure.countries.unknown == 0
    assert exposure.countries.coverage == 1


def test_a_line_without_a_breakdown_lowers_coverage_rather_than_being_spread():
    exposure = analyse_exposure(
        [("US1", D("0.6")), ("EU1", D("0.4"))], D("10000"), {"US1": _us_fund(), "EU1": _europe_fund()}
    )

    # The European fund has countries but no sectors.
    assert dict(exposure.sectors.shares) == {"technology": D("0.36"), "communication": D("0.24")}
    assert exposure.sectors.unknown == D("0.4")
    assert exposure.sectors.coverage == D("0.6")


def test_the_oldest_source_date_is_the_one_shown():
    exposure = analyse_exposure(
        [("US1", D("0.6")), ("EU1", D("0.4"))], D("10000"), {"US1": _us_fund(), "EU1": _europe_fund()}
    )

    assert exposure.countries.oldest_as_of == date(2026, 9, 30)


def test_fund_fees_are_the_value_of_each_line_times_its_fee():
    exposure = analyse_exposure(
        [("US1", D("0.6")), ("EU1", D("0.4"))], D("10000"), {"US1": _us_fund(), "EU1": _europe_fund()}
    )

    assert exposure.annual_fund_fees == D("6000") * D("0.003") + D("4000") * D("0.002")
    assert exposure.weighted_ter == D("0.0026")
    assert exposure.fee_coverage == 1


def test_fees_are_stated_over_the_lines_whose_fee_is_known_only():
    exposure = analyse_exposure([("US1", D("0.6")), ("XX", D("0.4"))], D("10000"), {"US1": _us_fund()})

    assert exposure.annual_fund_fees == D("18.000")
    assert exposure.weighted_ter == D("0.003")
    assert exposure.fee_coverage == D("0.6")


def test_no_known_fee_gives_no_figure_rather_than_zero():
    exposure = analyse_exposure([("XX", D("1"))], D("10000"), {})

    assert exposure.annual_fund_fees is None
    assert exposure.weighted_ter is None
    assert exposure.countries.unknown == 1


def test_an_empty_portfolio_has_no_exposure():
    assert analyse_exposure([], D("0"), {}) is None
