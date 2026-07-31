from datetime import date
from decimal import Decimal
from unittest.mock import patch

from models.enums import AssetType
from models.market import MarketAsset, MarketPriceHistory
from services.analytics.benchmark import (
    DEFAULT_BENCHMARK_ASSET_KEY,
    get_benchmark_series,
    resolve_benchmark_key,
)


class _Settings:
    def __init__(self, key):
        self.benchmark_asset_key = key


def test_resolve_falls_back_to_the_default_msci_world():
    assert resolve_benchmark_key(None) == DEFAULT_BENCHMARK_ASSET_KEY
    assert resolve_benchmark_key(_Settings(None)) == DEFAULT_BENCHMARK_ASSET_KEY
    assert resolve_benchmark_key(_Settings("  ")) == DEFAULT_BENCHMARK_ASSET_KEY


def test_resolve_uses_the_configured_key():
    assert resolve_benchmark_key(_Settings("IE00BK1PV551")) == "IE00BK1PV551"


def _seed(session, prices: dict[date, str]) -> None:
    asset = MarketAsset(
        asset_key="IE00B4L5Y983", symbol="IWDA.AS", name="MSCI World", asset_type=AssetType.STOCK
    )
    session.add(asset)
    session.commit()
    session.refresh(asset)
    for day, price in prices.items():
        session.add(
            MarketPriceHistory(market_asset_id=asset.id, price=Decimal(price), price_date=day)
        )
    session.commit()


@patch("services.analytics.benchmark.ensure_price_history")
def test_series_forward_fills_non_trading_days(_ensure, session):
    _seed(session, {date(2026, 1, 2): "100", date(2026, 1, 5): "102"})

    series = get_benchmark_series(session, "IE00B4L5Y983", date(2026, 1, 2), date(2026, 1, 6))

    assert series[date(2026, 1, 2)] == Decimal("100")
    assert series[date(2026, 1, 3)] == Decimal("100")
    assert series[date(2026, 1, 4)] == Decimal("100")
    assert series[date(2026, 1, 5)] == Decimal("102")
    assert series[date(2026, 1, 6)] == Decimal("102")


@patch("services.analytics.benchmark.ensure_price_history")
def test_a_window_opening_on_a_closed_day_is_seeded_from_the_last_prior_quote(_ensure, session):
    """Analysis windows start on the user's first buy, rarely a trading day.

    Without seeding from the previous quote the opening days would be missing,
    and the counterfactual would silently lose its starting point.
    """
    _seed(session, {date(2025, 12, 31): "99", date(2026, 1, 5): "102"})

    series = get_benchmark_series(session, "IE00B4L5Y983", date(2026, 1, 2), date(2026, 1, 5))

    assert series[date(2026, 1, 2)] == Decimal("99")
    assert series[date(2026, 1, 4)] == Decimal("99")
    assert series[date(2026, 1, 5)] == Decimal("102")


@patch("services.analytics.benchmark.ensure_price_history")
def test_unknown_asset_yields_an_empty_series(_ensure, session):
    assert get_benchmark_series(session, "NOPE", date(2026, 1, 2), date(2026, 1, 6)) == {}
