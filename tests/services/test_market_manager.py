import pytest
from unittest.mock import MagicMock
from decimal import Decimal
from typing import List, Dict

from services.market_data.manager import MarketDataManager
from services.market_data.providers.base import MarketDataProvider
from models.enums import AssetType

class MockProvider(MarketDataProvider):
    def __init__(self, data=None, supported_types=None):
        self.data = data or {}
        self.supported_types = supported_types or [AssetType.STOCK, AssetType.CRYPTO]

    def type_assets(self) -> List[AssetType]:
        return self.supported_types

    def get_info(self, symbol: str) -> Dict:
        if self.data and symbol in self.data:
            return self.data[symbol]
        return None

    def get_stock(self, symbol: str):
        info = self.get_info(symbol)
        return info["price"] if info else None

    def search(self, query: str) -> List[Dict]:
        return []

    def get_bulk_info(self, symbols: List[str]) -> Dict[str, Dict]:
        return {s: self.data.get(s) for s in symbols if s in self.data}

def test_manager_get_info_success_first_provider():
    mgr = MarketDataManager()
    mock_p1 = MockProvider({"BTC": {"price": Decimal("50000"), "name": "Bitcoin", "currency": "USD"}})
    mock_p2 = MockProvider({"BTC": {"price": Decimal("1")}})
    mgr.providers = [mock_p1, mock_p2]
    result = mgr.get_info("BTC", AssetType.CRYPTO)
    assert result["price"] == Decimal("50000")
    assert result["name"] == "Bitcoin"

def test_manager_get_info_fallback_second_provider():
    mgr = MarketDataManager()
    mock_p1 = MockProvider({})
    mock_p2 = MockProvider({"ETH": {"price": Decimal("3000"), "name": "Ethereum", "currency": "USD"}})
    mgr.providers = [mock_p1, mock_p2]
    result = mgr.get_info("ETH", AssetType.CRYPTO)
    assert result["price"] == Decimal("3000")

def test_manager_get_info_all_fail():
    mgr = MarketDataManager()
    mock_p1 = MockProvider({})
    mgr.providers = [mock_p1]
    result = mgr.get_info("UNKNOWN", AssetType.STOCK)
    assert result is None

def test_manager_get_price():
    mgr = MarketDataManager()
    mock_p1 = MockProvider({"SOL": {"price": Decimal("20.5"), "name": "Solana", "currency": "USD"}})
    mgr.providers = [mock_p1]
    price = mgr.get_price("SOL", AssetType.CRYPTO)
    assert price == Decimal("20.5")

def test_manager_get_price_none():
    mgr = MarketDataManager()
    mgr.providers = [MockProvider({})]
    assert mgr.get_price("MISSING", AssetType.STOCK) is None

def test_select_providers_default():
    mgr = MarketDataManager()
    result = mgr._select_providers(AssetType.STOCK)
    assert isinstance(result, list)
