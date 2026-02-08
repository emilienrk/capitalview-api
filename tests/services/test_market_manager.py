import pytest
from unittest.mock import MagicMock
from decimal import Decimal

from services.market_data.manager import MarketDataManager
from services.market_data.providers.base import MarketDataProvider

class MockProvider(MarketDataProvider):
    def __init__(self, data=None):
        self.data = data

    def get_info(self, symbol: str):
        if self.data and symbol in self.data:
            return self.data[symbol]
        return None

    def get_price(self, symbol: str):
        info = self.get_info(symbol)
        return info["price"] if info else None

def test_manager_get_info_success_first_provider():
    mgr = MarketDataManager()
    # Replace providers with Mock
    mock_p1 = MockProvider({"BTC": {"price": Decimal("50000"), "name": "Bitcoin", "currency": "USD"}})
    mock_p2 = MockProvider({"BTC": {"price": Decimal("1")}}) # Should not be called/used
    mgr.providers = [mock_p1, mock_p2]
    
    result = mgr.get_info("BTC")
    assert result["price"] == Decimal("50000")
    assert result["name"] == "Bitcoin"

def test_manager_get_info_fallback_second_provider():
    mgr = MarketDataManager()
    mock_p1 = MockProvider({}) # Empty
    mock_p2 = MockProvider({"ETH": {"price": Decimal("3000"), "name": "Ethereum", "currency": "USD"}})
    mgr.providers = [mock_p1, mock_p2]
    
    result = mgr.get_info("ETH")
    assert result["price"] == Decimal("3000")

def test_manager_get_info_all_fail():
    mgr = MarketDataManager()
    mock_p1 = MockProvider({})
    mgr.providers = [mock_p1]
    
    result = mgr.get_info("UNKNOWN")
    assert result is None

def test_manager_get_price():
    mgr = MarketDataManager()
    mock_p1 = MockProvider({"SOL": {"price": Decimal("20.5"), "name": "Solana", "currency": "USD"}})
    mgr.providers = [mock_p1]
    
    price = mgr.get_price("SOL")
    assert price == Decimal("20.5")

def test_manager_get_price_none():
    mgr = MarketDataManager()
    mgr.providers = [MockProvider({})]
    assert mgr.get_price("MISSING") is None

def test_select_provider_default():
    mgr = MarketDataManager()
    # Just verify it returns the list
    assert mgr._select_provider("ANY") == mgr.providers
