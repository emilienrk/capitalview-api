from typing import Optional, Dict, List
from decimal import Decimal
from .providers.base import MarketDataProvider
from .providers.yahoo import YahooProvider

class MarketDataManager:
    """
    Orchestrates data fetching from multiple providers.
    Implements Chain of Responsibility / Fallback logic.
    """
    def __init__(self):
        # Register providers here. Order matters for fallback.
        # Future: Add CoinGeckoProvider() for crypto specific handling
        self.providers: List[MarketDataProvider] = [
            YahooProvider()
        ]

    def _select_provider(self, symbol: str) -> List[MarketDataProvider]:
        """
        Select appropriate providers based on symbol format.
        Returns a list of providers to try in order.
        """
        # Example logic for future:
        # if "BTC" in symbol or "ETH" in symbol:
        #     return [self.coingecko, self.yahoo]
        return self.providers

    def get_info(self, symbol: str) -> Optional[Dict]:
        """
        Try to fetch info from registered providers.
        Returns the first successful result.
        """
        providers = self._select_provider(symbol)
        
        for provider in providers:
            data = provider.get_info(symbol)
            if data:
                return data
        
        return None

    def get_price(self, symbol: str) -> Optional[Decimal]:
        info = self.get_info(symbol)
        return info["price"] if info else None

# Singleton instance
market_data_manager = MarketDataManager()
