from typing import Optional, Dict, List
from datetime import date
from decimal import Decimal

from models.enums import AssetType
from .providers.base import MarketDataProvider
from .providers.yahoo import YahooProvider
from .providers.coinmarketcap import CoinMarketCapProvider
from .providers.coingecko import CoinGeckoProvider

class MarketDataManager:
    """
    Orchestrates data fetching from multiple providers.
    Implements Chain of Responsibility / Fallback logic.
    """
    def __init__(self):
        self.providers: List[MarketDataProvider] = [
            YahooProvider(),
            CoinMarketCapProvider(),
            CoinGeckoProvider(),
        ]

    def _select_providers(self, asset_type: AssetType) -> List[MarketDataProvider]:
        """
        Select appropriate providers based his sopported type asset.
        Returns a list of providers to try in order.
        """
        return [p for p in self.providers if asset_type in p.type_assets()]
    
    def get_info(self, symbol: str, asset_type: AssetType) -> Optional[Dict]:
        """
        Try to fetch info from registered providers.
        Returns the first successful result.
        """
        providers = self._select_providers(asset_type)
        
        for provider in providers:
            data = provider.get_info(symbol, asset_type)
            if data:
                return data
        
        return None

    def get_price(self, symbol: str, asset_type: AssetType) -> Optional[Decimal]:
        info = self.get_info(symbol, asset_type)
        return info["price"] if info else None

    def search(self, query: str, asset_type: AssetType) -> List[Dict]:
        """
        Search for assets across providers.
        """
        providers = self._select_providers(asset_type)

        for provider in providers:
            results = provider.search(query, asset_type)
            if results:
                return results
        return []

    def get_bulk_info(self, symbols: List[str], asset_type: AssetType) -> Dict[str, Dict]:
        """
        Fetch info for multiple symbols.
        """
        results = {}

        providers = self._select_providers(asset_type)

        for provider in providers:
            data = provider.get_bulk_info(symbols, asset_type)
            results.update(data)
            if len(results) == len(symbols):
                break
        
        return results

    def get_historical_prices(
        self, symbol: str, asset_type: AssetType, from_date: date, to_date: date
    ) -> dict[date, Decimal]:
        """
        Fetch daily historical prices across providers supporting the asset type.
        Returns the first non-empty result.
        """
        providers = self._select_providers(asset_type)
        for provider in providers:
            data = provider.get_historical_prices(symbol, from_date, to_date, asset_type)
            if data:
                return data
        return {}

market_data_manager = MarketDataManager()
