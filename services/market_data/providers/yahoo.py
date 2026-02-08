from decimal import Decimal
from typing import Optional, Dict
import yfinance as yf
from .base import MarketDataProvider

class YahooProvider(MarketDataProvider):
    """Yahoo Finance provider implementation."""

    def get_price(self, symbol: str) -> Optional[Decimal]:
        data = self.get_info(symbol)
        return data["price"] if data else None

    def get_info(self, symbol: str) -> Optional[Dict]:
        try:
            ticker = yf.Ticker(symbol)
            # Try fast_info first
            info = ticker.fast_info
            
            if not info or not hasattr(info, 'last_price'):
                 # Fallback to full info
                 data = ticker.info
                 current_price = data.get('currentPrice') or data.get('regularMarketPrice') or data.get('ask')
                 name = data.get('shortName') or data.get('longName') or symbol
                 currency = data.get('currency', 'EUR')
            else:
                 current_price = info.last_price
                 # Attempt to get extra details from full info only if needed, 
                 # or use defaults/metadata if accessible.
                 # For simplicity and robustness, we often need full info for Name/Currency initially.
                 # Optimization: access ticker.info lazy property
                 data = ticker.info
                 name = data.get('shortName') or data.get('longName') or symbol
                 currency = data.get('currency', 'EUR')

            if current_price is None:
                return None

            return {
                "price": Decimal(str(current_price)),
                "name": name,
                "currency": currency
            }
        except Exception as e:
            print(f"[YahooProvider] Error fetching {symbol}: {e}")
            return None
