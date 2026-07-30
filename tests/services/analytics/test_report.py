from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest

from services.analytics.report import build_investor_analytics

START = date(2024, 1, 1)


class _Tx:
    """Minimal stand-in for TransactionResponse — the report only reads these fields."""

    def __init__(self, tx_type, asset_key, amount, day, notes=None):
        self.type = tx_type
        self.asset_key = asset_key
        self.amount = Decimal(str(amount))
        self.executed_at = datetime(day.year, day.month, day.day, 10, 0)
        self.notes = notes


class _Snapshot:
    def __init__(self, day, total_value):
        self.snapshot_date = day
        self.total_value = Decimal(str(total_value))


class _Account:
    id = "acc_1"


def _run(snapshots, transactions, benchmark=None):
    """Drive the assembly with every data source stubbed out.

    The report is the only piece that joins settings, accounts, snapshots and the
    benchmark; stubbing them is what makes the gap branch reachable without
    seeding encrypted history rows.
    """
    with (
        patch("services.analytics.report.get_or_create_settings", return_value=None),
        patch("services.analytics.report.get_user_stock_accounts", return_value=[_Account()]),
        patch("services.analytics.report.get_account_transactions", return_value=transactions),
        patch("services.analytics.report.get_all_stock_accounts_history", return_value=snapshots),
        patch("services.analytics.report.get_benchmark_series", return_value=benchmark or {}),
    ):
        return build_investor_analytics(None, "user_1", "key")


def _two_year_series():
    """A portfolio funded once, growing steadily over two years."""
    return [_Snapshot(START + timedelta(days=n), 1000 + n) for n in range(0, 731)]


def test_a_single_snapshot_yields_no_gap():
    report = _run([_Snapshot(START, "1000")], [])

    assert report["days"] == 0
    assert report["investor_gap"] is None
    assert report["benchmark_asset_key"] == "IE00B4L5Y983"


def test_a_two_year_history_produces_a_gap_and_a_verdict():
    txs = [_Tx("DEPOSIT", "EUR", 1000, START)]
    report = _run(_two_year_series(), txs)

    gap = report["investor_gap"]
    assert report["days"] == 730
    # Two years clears the 180-day minimum but not the 1095-day solid threshold.
    assert gap["twr"]["reliability"] == "indicatif"
    assert gap["twr"]["value"] > Decimal("0")
    assert gap["mwr"]["value"] is not None
    assert gap["gap"]["value"] == gap["mwr"]["value"] - gap["twr_annualised"]["value"]
    assert gap["auto_provision_share"] == Decimal("0")
    assert gap["verdict"]


def test_a_short_history_withholds_every_value():
    snapshots = [_Snapshot(START + timedelta(days=n), 1000 + n) for n in range(0, 31)]
    report = _run(snapshots, [_Tx("DEPOSIT", "EUR", 1000, START)])

    gap = report["investor_gap"]
    assert report["days"] == 30
    # The numbers are computable; the gate is what keeps them off the wire.
    for key in ("twr", "twr_annualised", "mwr", "gap", "gap_eur"):
        assert gap[key]["value"] is None
        assert gap[key]["reliability"] == "insuffisant"
    assert gap["verdict"].startswith("Pas encore assez d'historique")


def test_auto_provisions_are_excluded_from_the_money_weighted_flows():
    """An auto-provision is not a real transfer, so it must not move the MWR."""
    series = _two_year_series()
    real_only = _run(series, [_Tx("DEPOSIT", "EUR", 1000, START)])
    with_auto = _run(
        series,
        [
            _Tx("DEPOSIT", "EUR", 1000, START),
            _Tx("DEPOSIT", "EUR", 500, START + timedelta(days=365), notes="Provision automatique"),
        ],
    )

    assert with_auto["investor_gap"]["mwr"]["value"] == real_only["investor_gap"]["mwr"]["value"]
    # It is still reported, because it bounds how much the gap can be trusted.
    assert with_auto["investor_gap"]["auto_provision_share"] == Decimal("0.3333")


def test_the_benchmark_uses_the_first_and_last_quote_of_the_window():
    series = _two_year_series()
    quotes = {series[0].snapshot_date: Decimal("100"), series[-1].snapshot_date: Decimal("121")}
    report = _run(series, [_Tx("DEPOSIT", "EUR", 1000, START)], benchmark=quotes)

    # +21% over 730 days is 10% a year.
    assert report["investor_gap"]["benchmark_annualised"]["value"] == pytest.approx(
        Decimal("0.10"), abs=1e-9
    )
