import pytest
from unittest.mock import MagicMock
from decimal import Decimal
import sys
import yfinance as yf

from services.market_data.providers.yahoo import YahooProvider

@pytest.fixture
def provider():
    return YahooProvider()

def test_yahoo_get_info_success_fast_info(provider):
    mock_symbol = MagicMock()
    mock_symbol.fast_info.last_price = 150.0
    mock_symbol.info = {"shortName": "Apple Inc.", "currency": "USD"}
    yf.Ticker.return_value = mock_symbol
    result = provider.get_info("AAPL")
    assert result is not None
    assert result["price"] == Decimal("150.0")
    assert result["name"] == "Apple Inc."
    assert result["currency"] == "USD"

def test_yahoo_get_info_fallback_regular_market_price(provider):
    mock_symbol = MagicMock()
    mock_symbol.fast_info = None
    mock_symbol.info = {"regularMarketPrice": 200.5, "longName": "Microsoft Corp", "currency": "EUR"}
    yf.Ticker.return_value = mock_symbol
    result = provider.get_info("MSFT")
    assert result["price"] == Decimal("200.5")
    assert result["name"] == "Microsoft Corp"
    assert result["currency"] == "EUR"

def test_yahoo_get_info_fallback_current_price(provider):
    mock_symbol = MagicMock()
    mock_symbol.fast_info = MagicMock()
    del mock_symbol.fast_info.last_price
    mock_symbol.info = {"currentPrice": 300.0, "shortName": "NVDA"}
    yf.Ticker.return_value = mock_symbol
    result = provider.get_info("NVDA")
    assert result["price"] == Decimal("300.0")
    assert result["name"] == "NVDA"

def test_yahoo_get_info_fallback_symbol_name(provider):
    mock_symbol = MagicMock()
    mock_symbol.fast_info = None
    mock_symbol.info = {"currentPrice": 10.0}
    yf.Ticker.return_value = mock_symbol
    result = provider.get_info("UNKNOWN")
    assert result["name"] == "UNKNOWN"

def test_yahoo_get_info_missing_price(provider):
    mock_symbol = MagicMock()
    mock_symbol.fast_info = None
    mock_symbol.info = {"shortName": "No Price Co"}
    yf.Ticker.return_value = mock_symbol
    result = provider.get_info("NOPRICE")
    assert result is None

def test_yahoo_get_info_exception(provider):
    yf.Ticker.side_effect = Exception("API Error")
    result = provider.get_info("ERR")
    assert result is None
    yf.Ticker.side_effect = None

