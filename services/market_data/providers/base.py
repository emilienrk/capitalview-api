from abc import ABC, abstractmethod
from decimal import Decimal
from typing import List, Optional

from models.enums import AssetType

class MarketDataProvider(ABC):
    """
    Abstract base class for market data providers.
    Defines the standard interface for fetching asset prices and information.
    """

    @abstractmethod
    def type_assets(self) -> List[AssetType]:
        """
        Return type of supprted assets.
            
        Returns:
            List of assets supported
        """
        pass

    @abstractmethod
    def get_stock(self, symbol: str) -> Optional[Decimal]:
        """
        Fetch the current price for a single symbol.
        
        Args:
            symbol: The symbol symbol (e.g., 'AAPL', 'BTC-USD').
            
        Returns:
            Decimal: The current price.
            None: If the symbol is not found or data is unavailable.
        """
        pass

    @abstractmethod
    def get_info(self, symbol: str) -> Optional[dict]:
        """
        Fetch detailed information for a symbol.
        
        Args:
            symbol: The symbol symbol.
            
        Returns:
            dict: A dictionary containing at least:
                - 'name': str
                - 'currency': str
                - 'price': Decimal
            None: If fetching fails.
        """
        pass

    @abstractmethod
    def search(self, query: str) -> list[dict]:
        """
        Search for assets matching a query string.
        
        Args:
            query: The search term.
            
        Returns:
            list[dict]: A list of results, each containing:
                - 'symbol': str
                - 'name': str
                - 'exchange': str (optional)
                - 'type': str (optional)
                - 'currency': str (optional)
        """
        pass

    @abstractmethod
    def get_bulk_info(self, symbols: list[str]) -> dict[str, dict]:
        """
        Fetch information for multiple symbols in a single batch operation.
        
        Args:
            symbols: A list of symbol symbols.
            
        Returns:
            dict[str, dict]: A mapping of symbol -> info dict (same structure as get_info).
                             Symbols that fail to fetch are excluded from the result.
        """
        pass