import pytest
from unittest.mock import MagicMock
from decimal import Decimal
import sys

# yfinance is already mocked in conftest.py, so we import it to configure the mock
import yfinance as yf

from services.market_data.providers.yahoo import YahooProvider

@pytest.fixture
def provider():
    return YahooProvider()

def test_yahoo_get_info_success_fast_info(provider):
    # Setup Mock
    mock_ticker = MagicMock()
    # fast_info has last_price
    mock_ticker.fast_info.last_price = 150.0
    # info has name/currency
    mock_ticker.info = {
        "shortName": "Apple Inc.",
        "currency": "USD"
    }
    
    yf.Ticker.return_value = mock_ticker
    
    result = provider.get_info("AAPL")
    
    assert result is not None
    assert result["price"] == Decimal("150.0")
    assert result["name"] == "Apple Inc."
    assert result["currency"] == "USD"

def test_yahoo_get_info_fallback_regular_market_price(provider):
    # Setup Mock where fast_info fails or missing
    mock_ticker = MagicMock()
    # fast_info is empty or raises AttributeError on access? 
    # The code checks: if not info or not hasattr(info, 'last_price')
    mock_ticker.fast_info = None
    
    mock_ticker.info = {
        "regularMarketPrice": 200.5,
        "longName": "Microsoft Corp",
        "currency": "EUR"
    }
    
    yf.Ticker.return_value = mock_ticker
    
    result = provider.get_info("MSFT")
    
    assert result["price"] == Decimal("200.5")
    assert result["name"] == "Microsoft Corp"
    assert result["currency"] == "EUR"

def test_yahoo_get_info_fallback_current_price(provider):
    mock_ticker = MagicMock()
    mock_ticker.fast_info = MagicMock()
    del mock_ticker.fast_info.last_price # Simulate missing attribute
    
    mock_ticker.info = {
        "currentPrice": 300.0,
        "shortName": "NVDA"
    }
    
    yf.Ticker.return_value = mock_ticker
    result = provider.get_info("NVDA")
    assert result["price"] == Decimal("300.0")
    assert result["name"] == "NVDA" # fallback to symbol? No, shortName is present

def test_yahoo_get_info_fallback_symbol_name(provider):
    mock_ticker = MagicMock()
    mock_ticker.fast_info = None
    mock_ticker.info = {
        "currentPrice": 10.0,
        # No name fields
    }
    yf.Ticker.return_value = mock_ticker
    
    result = provider.get_info("UNKNOWN")
    assert result["name"] == "UNKNOWN" # Fallback to symbol

def test_yahoo_get_info_missing_price(provider):
    mock_ticker = MagicMock()
    mock_ticker.fast_info = None
    mock_ticker.info = {
        "shortName": "No Price Co"
    }
    yf.Ticker.return_value = mock_ticker
    
    result = provider.get_info("NOPRICE")
    assert result is None

def test_yahoo_get_info_exception(provider):
    # Simulate constructor raising exception
    yf.Ticker.side_effect = Exception("API Error")
    
    result = provider.get_info("ERR")
    assert result is None
    
    # Reset side_effect for other tests
    yf.Ticker.side_effect = None

def test_yahoo_get_price(provider):
    # Reuse successful setup via get_info
    mock_ticker = MagicMock()
    mock_ticker.fast_info.last_price = 100.0
    mock_ticker.info = {}
    yf.Ticker.return_value = mock_ticker
    
    price = provider.get_price("TEST")
    assert price == Decimal("100.0")

def test_yahoo_get_price_none(provider):
    yf.Ticker.side_effect = Exception("Fail")
    price = provider.get_price("FAIL")
    assert price is None
    yf.Ticker.side_effect = None
