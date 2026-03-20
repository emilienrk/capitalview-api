"""
Tests for exchange-calendar-aware cache invalidation logic.

Covered functions:
  - _get_calendar
  - _is_market_open
  - _last_market_close
  - _is_cache_fresh
  - get_stock_price (integration: market closed → DB-only path)
"""

import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

# ── yfinance must be mocked before any service import ─────────────────────────
if "yfinance" not in sys.modules:
    sys.modules["yfinance"] = MagicMock()

from sqlmodel import Session, select

from models.enums import AssetType
from models.market import MarketAsset, MarketPriceHistory
from services.market import (
    CACHE_DURATION,
    _get_calendar,
    _is_cache_fresh,
    _is_market_open,
    _last_market_close,
    _upsert_price,
    get_stock_price,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_asset(session, *, isin, symbol, name, asset_type=AssetType.STOCK, exchange=None):
    ma = MarketAsset(isin=isin, symbol=symbol, name=name, asset_type=asset_type, exchange=exchange)
    session.add(ma)
    session.commit()
    session.refresh(ma)
    return ma


def _make_price(session, asset_id, price, *, updated_at=None):
    _upsert_price(session, asset_id, price)
    session.commit()
    if updated_at is not None:
        entry = session.exec(
            select(MarketPriceHistory).where(
                MarketPriceHistory.market_asset_id == asset_id,
                MarketPriceHistory.price_date == date.today(),
            )
        ).first()
        entry.updated_at = updated_at
        session.add(entry)
        session.commit()


def _price_entry(session, asset_id, price, updated_at):
    """Return a transient MarketPriceHistory object (not persisted) for unit tests."""
    entry = MarketPriceHistory(
        market_asset_id=asset_id,
        price=price,
        price_date=date.today(),
    )
    entry.updated_at = updated_at
    return entry


# ---------------------------------------------------------------------------
# _get_calendar
# ---------------------------------------------------------------------------


class TestGetCalendar:
    def test_known_mic_returns_calendar(self):
        cal = _get_calendar("XPAR")
        assert cal is not None

    def test_known_mic_xnys(self):
        cal = _get_calendar("XNYS")
        assert cal is not None

    def test_unknown_mic_returns_none(self):
        cal = _get_calendar("XXXX_UNKNOWN_MIC")
        assert cal is None

    def test_empty_string_returns_none(self):
        cal = _get_calendar("")
        assert cal is None


# ---------------------------------------------------------------------------
# _is_market_open
# ---------------------------------------------------------------------------


class TestIsMarketOpen:
    def test_unknown_mic_returns_true(self):
        # Unknown exchange → conservative fallback: assume open
        assert _is_market_open("XXXX_UNKNOWN") is True

    def test_known_mic_returns_bool(self):
        result = _is_market_open("XPAR")
        assert isinstance(result, bool)

    def test_market_always_closed_on_weekend(self):
        # Saturday UTC
        saturday = pd.Timestamp("2026-03-21 12:00:00", tz="UTC")
        with patch("services.market.pd") as mock_pd:
            mock_pd.Timestamp.now.return_value = saturday
            # We need the real cal.is_open_on_minute to work, so patch only Timestamp.now
            import exchange_calendars as ec
            cal = ec.get_calendar("XPAR")
            assert not cal.is_open_on_minute(saturday)

    def test_market_open_midday_tuesday(self):
        # Tuesday 12:00 Paris local = 11:00 UTC, well within XPAR session
        import exchange_calendars as ec
        cal = ec.get_calendar("XPAR")
        tuesday_open = pd.Timestamp("2026-03-17 11:00:00", tz="UTC")
        assert cal.is_open_on_minute(tuesday_open)

    def test_market_closed_early_morning(self):
        import exchange_calendars as ec
        cal = ec.get_calendar("XPAR")
        early = pd.Timestamp("2026-03-17 03:00:00", tz="UTC")  # 04:00 Paris, before open
        assert not cal.is_open_on_minute(early)


# ---------------------------------------------------------------------------
# _last_market_close
# ---------------------------------------------------------------------------


class TestLastMarketClose:
    def test_unknown_mic_returns_none(self):
        assert _last_market_close("XXXX_UNKNOWN") is None

    def test_known_mic_returns_datetime(self):
        result = _last_market_close("XPAR")
        assert result is not None
        assert isinstance(result, datetime)
        assert result.tzinfo is not None  # must be tz-aware

    def test_last_close_in_the_past(self):
        result = _last_market_close("XNYS")
        assert result < datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# _is_cache_fresh — unit tests (asset + price_entry provided directly)
# ---------------------------------------------------------------------------


FRESH = datetime.now(timezone.utc) - timedelta(minutes=5)
STALE = datetime.now(timezone.utc) - CACHE_DURATION - timedelta(minutes=5)
# A time clearly after any exchange's last close (3 min ago)
AFTER_LAST_CLOSE = datetime.now(timezone.utc) - timedelta(minutes=3)

_NOW = datetime.now(timezone.utc)  # reference "now" for tests using _now injection


class TestIsPriceStillValidCrypto:
    """Crypto: 24/7, purely time-based."""

    def _asset(self, session):
        return _make_asset(
            session, isin="BTC", symbol="BTC", name="Bitcoin", asset_type=AssetType.CRYPTO, exchange=None
        )

    def test_fresh_cache_valid(self, session: Session):
        ma = self._asset(session)
        entry = _price_entry(session, ma.id, Decimal("50000"), _NOW - timedelta(minutes=5))
        assert _is_cache_fresh(ma, entry, _now=_NOW) is True

    def test_stale_cache_invalid(self, session: Session):
        ma = self._asset(session)
        entry = _price_entry(session, ma.id, Decimal("50000"), _NOW - CACHE_DURATION - timedelta(minutes=5))
        assert _is_cache_fresh(ma, entry, _now=_NOW) is False


class TestIsPriceStillValidFiat:
    """Fiat: forex, open Mon–Fri, closed on weekends."""

    def _asset(self, session):
        return _make_asset(
            session, isin="USD", symbol="USD", name="Dollar", asset_type=AssetType.FIAT, exchange=None
        )

    def test_fresh_weekday_valid(self, session: Session):
        ma = self._asset(session)
        monday = datetime(2026, 3, 16, 12, 0, tzinfo=timezone.utc)
        updated = monday - timedelta(minutes=5)
        entry = _price_entry(session, ma.id, Decimal("0.92"), updated)
        assert _is_cache_fresh(ma, entry, _now=monday) is True

    def test_stale_weekday_invalid(self, session: Session):
        ma = self._asset(session)
        monday = datetime(2026, 3, 16, 12, 0, tzinfo=timezone.utc)
        updated = monday - CACHE_DURATION - timedelta(minutes=5)
        entry = _price_entry(session, ma.id, Decimal("0.92"), updated)
        assert _is_cache_fresh(ma, entry, _now=monday) is False

    def test_weekend_always_valid(self, session: Session):
        ma = self._asset(session)
        saturday = datetime(2026, 3, 21, 12, 0, tzinfo=timezone.utc)
        # Even an update 48h ago → valid on weekend (forex closed)
        updated = saturday - timedelta(hours=48)
        entry = _price_entry(session, ma.id, Decimal("0.92"), updated)
        assert _is_cache_fresh(ma, entry, _now=saturday) is True

    def test_sunday_always_valid(self, session: Session):
        ma = self._asset(session)
        sunday = datetime(2026, 3, 22, 12, 0, tzinfo=timezone.utc)
        updated = sunday - timedelta(hours=24)
        entry = _price_entry(session, ma.id, Decimal("0.92"), updated)
        assert _is_cache_fresh(ma, entry, _now=sunday) is True


class TestIsPriceStillValidStock:
    """Stock with a known MIC: market open → hourly; market closed → session-based."""

    def _asset(self, session, exchange="XPAR"):
        return _make_asset(
            session, isin="FR0000131104", symbol="BNP.PA", name="BNP Paribas",
            asset_type=AssetType.STOCK, exchange=exchange,
        )

    def test_market_open_fresh_valid(self, session: Session):
        ma = self._asset(session)
        entry = _price_entry(session, ma.id, Decimal("60"), _NOW - timedelta(minutes=5))
        with patch("services.market._is_market_open", return_value=True):
            assert _is_cache_fresh(ma, entry, _now=_NOW) is True

    def test_market_open_stale_invalid(self, session: Session):
        ma = self._asset(session)
        entry = _price_entry(session, ma.id, Decimal("60"), _NOW - CACHE_DURATION - timedelta(minutes=5))
        with patch("services.market._is_market_open", return_value=True):
            assert _is_cache_fresh(ma, entry, _now=_NOW) is False

    def test_market_closed_updated_after_last_close_valid(self, session: Session):
        ma = self._asset(session)
        last_close = datetime.now(timezone.utc) - timedelta(hours=2)
        updated_after_close = last_close + timedelta(minutes=30)
        entry = _price_entry(session, ma.id, Decimal("60"), updated_after_close)
        with patch("services.market._is_market_open", return_value=False):
            with patch("services.market._last_market_close", return_value=last_close):
                assert _is_cache_fresh(ma, entry) is True

    def test_market_closed_updated_before_last_close_invalid(self, session: Session):
        ma = self._asset(session)
        last_close = datetime.now(timezone.utc) - timedelta(hours=2)
        updated_before_close = last_close - timedelta(hours=1)
        entry = _price_entry(session, ma.id, Decimal("60"), updated_before_close)
        with patch("services.market._is_market_open", return_value=False):
            with patch("services.market._last_market_close", return_value=last_close):
                assert _is_cache_fresh(ma, entry) is False

    def test_market_closed_unknown_last_close_falls_back_to_hourly_fresh(self, session: Session):
        ma = self._asset(session)
        entry = _price_entry(session, ma.id, Decimal("60"), _NOW - timedelta(minutes=5))
        with patch("services.market._is_market_open", return_value=False):
            with patch("services.market._last_market_close", return_value=None):
                assert _is_cache_fresh(ma, entry, _now=_NOW) is True

    def test_market_closed_unknown_last_close_falls_back_to_hourly_stale(self, session: Session):
        ma = self._asset(session)
        entry = _price_entry(session, ma.id, Decimal("60"), _NOW - CACHE_DURATION - timedelta(minutes=5))
        with patch("services.market._is_market_open", return_value=False):
            with patch("services.market._last_market_close", return_value=None):
                assert _is_cache_fresh(ma, entry, _now=_NOW) is False

    def test_unknown_exchange_falls_back_to_hourly(self, session: Session):
        """Asset with no exchange field → behaves like crypto (hourly TTL)."""
        ma = _make_asset(
            session, isin="XX0000000000", symbol="XX", name="Unknown",
            asset_type=AssetType.STOCK, exchange=None,
        )
        entry_fresh = _price_entry(session, ma.id, Decimal("100"), _NOW - timedelta(minutes=5))
        entry_stale = _price_entry(session, ma.id, Decimal("100"), _NOW - CACHE_DURATION - timedelta(minutes=5))
        assert _is_cache_fresh(ma, entry_fresh, _now=_NOW) is True
        assert _is_cache_fresh(ma, entry_stale, _now=_NOW) is False


# ---------------------------------------------------------------------------
# Integration: get_stock_price does NOT call API when market is closed + fresh data
# ---------------------------------------------------------------------------


class TestGetStockPriceMarketClosedIntegration:
    @pytest.fixture
    def mock_market_manager(self):
        with patch("services.market.market_data_manager") as mock:
            yield mock

    def test_no_api_call_when_market_closed_and_price_fresh(
        self, session: Session, mock_market_manager
    ):
        """
        When the market is closed and the cached price was updated after the last
        session close, get_stock_price must return the DB value without calling the API.
        """
        last_close = datetime.now(timezone.utc) - timedelta(hours=3)
        updated_after_close = last_close + timedelta(hours=1)

        ma = _make_asset(
            session, isin="FR0000131104", symbol="BNP.PA", name="BNP Paribas",
            asset_type=AssetType.STOCK, exchange="XPAR",
        )
        _make_price(session, ma.id, Decimal("60.00"), updated_at=updated_after_close)

        with patch("services.market._is_market_open", return_value=False):
            with patch("services.market._last_market_close", return_value=last_close):
                result = get_stock_price(session, "FR0000131104")

        assert result == Decimal("60.00")
        mock_market_manager.get_info.assert_not_called()

    def test_api_called_when_market_closed_but_price_predates_last_close(
        self, session: Session, mock_market_manager
    ):
        """
        When the cached price was updated BEFORE the last session close, the data
        is stale and the API must be called to get the closing price.
        """
        last_close = datetime.now(timezone.utc) - timedelta(hours=2)
        updated_before_close = last_close - timedelta(hours=1)
        new_price = Decimal("61.50")

        ma = _make_asset(
            session, isin="FR0000131105", symbol="BNP.PA", name="BNP Paribas",
            asset_type=AssetType.STOCK, exchange="XPAR",
        )
        _make_price(session, ma.id, Decimal("60.00"), updated_at=updated_before_close)

        mock_market_manager.get_info.return_value = {
            "name": "BNP Paribas",
            "price": new_price,
            "currency": "EUR",
            "symbol": "BNP.PA",
        }

        with patch("services.market._is_market_open", return_value=False):
            with patch("services.market._last_market_close", return_value=last_close):
                with patch("services.market.get_exchange_rate", return_value=Decimal("1.0")):
                    result = get_stock_price(session, "FR0000131105")

        assert result == new_price
        mock_market_manager.get_info.assert_called_once()

    def test_api_called_when_market_open_and_cache_stale(
        self, session: Session, mock_market_manager
    ):
        """When the market is open, honour the hourly TTL even if price exists in DB."""
        stale_time = datetime.now(timezone.utc) - CACHE_DURATION - timedelta(minutes=10)
        new_price = Decimal("62.00")

        ma = _make_asset(
            session, isin="FR0000131106", symbol="BNP.PA", name="BNP Paribas",
            asset_type=AssetType.STOCK, exchange="XPAR",
        )
        _make_price(session, ma.id, Decimal("60.00"), updated_at=stale_time)

        mock_market_manager.get_info.return_value = {
            "name": "BNP Paribas",
            "price": new_price,
            "currency": "EUR",
            "symbol": "BNP.PA",
        }

        with patch("services.market._is_market_open", return_value=True):
            with patch("services.market.get_exchange_rate", return_value=Decimal("1.0")):
                result = get_stock_price(session, "FR0000131106")

        assert result == new_price
        mock_market_manager.get_info.assert_called_once()
