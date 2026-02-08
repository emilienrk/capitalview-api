from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Optional, Dict

class MarketDataProvider(ABC):
    """Abstract base class for market data providers."""

    @abstractmethod
    def get_price(self, symbol: str) -> Optional[Decimal]:
        """Fetch current price for a symbol."""
        pass

    @abstractmethod
    def get_info(self, symbol: str) -> Optional[Dict]:
        """
        Fetch detailed info (name, currency, price).
        Returns dict with keys: 'name', 'currency', 'price'.
        """
        pass
