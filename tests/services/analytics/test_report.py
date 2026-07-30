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


# ---------------------------------------------------------------------------
# M2 blocks: counterfactual bridge and execution cost
# ---------------------------------------------------------------------------

BENCH = "IE00B4L5Y983"


class _Buy:
    def __init__(self, asset_key, day, amount="10", price="100", fees="0"):
        self.type = "BUY"
        self.asset_key = asset_key
        self.amount = Decimal(amount)
        self.price_per_unit = Decimal(price)
        self.fees = Decimal(fees)
        self.executed_at = datetime(day.year, day.month, day.day, 10, 0)
        self.notes = None
        self.currency = "EUR"


class _Cash:
    def __init__(self, tx_type, day, amount="10000"):
        self.type = tx_type
        self.asset_key = "EUR"
        self.amount = Decimal(amount)
        self.price_per_unit = Decimal("1")
        self.fees = Decimal("0")
        self.executed_at = datetime(day.year, day.month, day.day, 10, 0)
        self.notes = None
        self.currency = "EUR"


def _monthly_buys(months: int, price="100", start=START):
    """One purchase a month, plus the deposit that funds them."""
    txs = [_Cash("DEPOSIT", start, amount=str(1000 * months + 5000))]
    for n in range(months):
        day = date(start.year + (start.month - 1 + n) // 12, (start.month - 1 + n) % 12 + 1, 5)
        txs.append(_Buy(BENCH, day, price=price))
    return txs


def _quotes(transactions, price="100"):
    """A flat sparse series covering every day the window can ask about."""
    days = sorted({tx.executed_at.date() for tx in transactions})
    first, last = days[0], date.today()
    span = [first + timedelta(days=n) for n in range((last - first).days + 1)]
    return {BENCH: {d: Decimal(price) for d in span}}


def _run_blocks(transactions, snapshots=None, window_end=None):
    """Drive the report with the market layer stubbed but the blocks really running."""
    matrix = _quotes(transactions)
    snapshots = snapshots if snapshots is not None else _two_year_series()

    with (
        patch("services.analytics.report.get_or_create_settings", return_value=None),
        patch("services.analytics.report.get_user_stock_accounts", return_value=[_Account()]),
        patch("services.analytics.report.get_account_transactions", return_value=transactions),
        patch("services.analytics.report.get_all_stock_accounts_history", return_value=snapshots),
        patch("services.analytics.report.get_benchmark_series", return_value={}),
        patch("services.analytics.window.ensure_price_history"),
        patch("services.analytics.window.get_historical_exchange_rates_db"),
        patch("services.analytics.window.first_quote_date", return_value=date(2010, 1, 1)),
        patch("services.analytics.report.get_price_matrix", return_value=matrix),
        patch("services.analytics.report.fill_price_gaps", side_effect=lambda m, *_a, **_k: m),
        # No exchange calendar here: execution falls back to quoted days, which is
        # what the fixture provides.
        patch("services.analytics.report.resolve_trading_days", return_value={}),
    ):
        return build_investor_analytics(None, "user_1", "key")


def test_both_new_blocks_are_absent_without_any_purchase():
    report = _run_blocks([_Cash("DEPOSIT", START)])

    assert report["counterfactual"] is None
    assert report["execution"] is None


def test_a_two_year_buying_history_produces_both_blocks():
    report = _run_blocks(_monthly_buys(24))

    bridge = report["counterfactual"]
    execution = report["execution"]

    assert bridge is not None and execution is not None
    assert bridge["order"] == ["timing", "selection", "execution", "fees", "exits"]
    assert execution["order_count"] == 24
    assert bridge["verdict"] and execution["verdict"]


def test_the_bridge_payload_reconciles():
    bridge = _run_blocks(_monthly_buys(24))["counterfactual"]

    total = bridge["baseline"] + sum(s["amount"] for s in bridge["steps"]) + bridge["residual"]
    assert abs(total - bridge["final"]) < Decimal("0.01")


def test_too_few_orders_withholds_the_value_and_the_verdict_follows():
    """The M1 trap: a verdict must never assert what the gate just withheld."""
    report = _run_blocks(_monthly_buys(5))

    execution = report["execution"]
    assert execution["order_count"] == 5
    assert execution["slippage_bps"]["value"] is None
    assert execution["slippage_bps"]["reliability"] == "insuffisant"
    assert execution["cost_eur"]["value"] is None
    assert execution["verdict"].startswith("Seulement 5 achats")


def test_a_withheld_metric_also_withholds_its_distribution():
    """A box plot is the same numbers in another shape — it follows the gate."""
    execution = _run_blocks(_monthly_buys(5))["execution"]

    assert execution["distribution"] is None


def test_a_solid_sample_exposes_the_distribution():
    execution = _run_blocks(_monthly_buys(24))["execution"]

    assert execution["distribution"] is not None
    assert set(execution["distribution"]) == {"minimum", "q1", "median", "q3", "maximum"}


def test_buying_at_a_flat_price_is_not_detectable():
    """Every purchase at the month average: nothing to report either way."""
    execution = _run_blocks(_monthly_buys(24))["execution"]

    assert execution["is_detectable"] is False
    assert "pas là qu'il faut chercher" in execution["verdict"]


def test_the_replay_blocks_survive_missing_snapshots():
    """Snapshots are rebuilt asynchronously; the replay blocks do not need them."""
    report = _run_blocks(_monthly_buys(24), snapshots=[])

    assert report["investor_gap"] is None
    assert report["counterfactual"] is not None
    assert report["execution"] is not None


# ── M3 · behaviour blocks and the global verdict ──────────────────────


def test_the_new_blocks_are_assembled_for_a_two_year_buyer():
    report = _run_blocks(_monthly_buys(24))

    regularity = report["regularity"]
    assert regularity["months_invested"] == 24
    assert regularity["equivalent_monthly_purchases"]["value"] is not None
    assert len(regularity["monthly"]) == regularity["months_total"]
    assert report["deposit_lag"] is not None
    assert report["market_conditioning"] is not None


def test_too_few_purchases_withholds_every_regularity_value_and_its_heatmap():
    # Two purchases is under the three-purchase floor: a window can be long and
    # still hold no rhythm to read.
    report = _run_blocks(_monthly_buys(2))

    regularity = report["regularity"]
    assert regularity["equivalent_monthly_purchases"]["value"] is None
    assert regularity["temporal_hhi"]["value"] is None
    assert regularity["invested_share"]["value"] is None
    # A heatmap is the same numbers in another shape: withheld here too.
    assert regularity["monthly"] == []
    assert "pas encore de quoi dire" in regularity["verdict"]


def test_the_conditioning_block_withholds_its_chart_data_when_gated():
    report = _run_blocks(_monthly_buys(6))

    conditioning = report["market_conditioning"]
    if conditioning is not None:
        assert conditioning["weighted_drawdown"]["value"] is None
        assert conditioning["density"] == []
        assert conditioning["points"] == []
        assert conditioning["p_value"] is None


def test_the_global_verdict_falls_back_when_nothing_passed_its_gate():
    report = _run_blocks([_Cash("DEPOSIT", START)], snapshots=[])

    assert "Pas encore assez d'historique" in report["verdict"]


def test_the_global_verdict_never_quotes_a_withheld_number():
    report = _run_blocks(_monthly_buys(2))

    regularity = report["regularity"]
    assert regularity["equivalent_monthly_purchases"]["value"] is None
    # The equivalent-purchases sentence is the one this block contributes; with
    # the value withheld, none of its wording may appear.
    assert "achats mensuels égaux" not in report["verdict"]


def test_a_flat_response_still_carries_a_verdict_string():
    report = _run([_Snapshot(START, "1000")], [])

    assert isinstance(report["verdict"], str)
    assert report["verdict"]
