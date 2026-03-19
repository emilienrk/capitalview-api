from abc import ABC, abstractmethod
from datetime import date
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
    def get_info(self, symbol: str, asset_type: Optional[AssetType] = None) -> Optional[dict]:
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
    def search(self, query: str, asset_type: Optional[AssetType] = None) -> list[dict]:
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
    def get_bulk_info(self, symbols: list[str], asset_type: Optional[AssetType] = None) -> dict[str, dict]:
        """
        Fetch information for multiple symbols in a single batch operation.
        
        Args:
            symbols: A list of symbol symbols.
            
        Returns:
            dict[str, dict]: A mapping of symbol -> info dict (same structure as get_info).
                             Symbols that fail to fetch are excluded from the result.
        """
        pass

    _exchange_codes: dict[str, str] = {}

    def normalize_exchange(self, code: str | None) -> str | None:
        """
        Translate a provider-specific exchange code to a canonical market name.

        Looks up self._exchange_codes, which each provider declares as a class
        attribute with its own code vocabulary.  Unknown codes are returned
        unchanged so no information is silently dropped.
        """
        if code is None:
            return None
        return self._exchange_codes.get(code, code) or None

    def get_historical_prices(
        self, symbol: str, from_date: date, to_date: date, asset_type: Optional[AssetType] = None
    ) -> dict[date, Decimal]:
        """
        Fetch daily closing prices for a symbol over a date range.

        Args:
            symbol: The asset symbol.
            from_date: First day (inclusive).
            to_date: Last day (inclusive).

        Returns:
            dict mapping each calendar date with a price to its closing Decimal value.
            Dates with no data are omitted.
            Providers that don't support historical data return an empty dict.
        """
        return {}