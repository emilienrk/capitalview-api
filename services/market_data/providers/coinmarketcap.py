from decimal import Decimal
from typing import Optional, Dict, List

from models.enums import AssetType
import requests

from config import get_settings
from .base import MarketDataProvider


class CoinMarketCapProvider(MarketDataProvider):
    def __init__(self):
        self.settings = get_settings()
        self.api_url = self.settings.cmc_api_url
        self.api_key = self.settings.cmc_api_key

    def type_assets(self) -> List[AssetType]:
        return [AssetType.CRYPTO]

    def get_stock(self, symbol: str) -> Optional[Decimal]:
        return None

    def get_info(self, symbol: str) -> Optional[Dict]:
        if not symbol or not symbol.strip() or not self.api_key:
            return None
            
        try:
            headers = {
                "X-CMC_PRO_API_KEY": self.api_key,
                "Accept": "application/json"
            }
            
            params = {
                "symbol": symbol.upper(),
                "convert": "EUR"
            }
            
            response = requests.get(
                f"{self.api_url}/v2/cryptocurrency/quotes/latest",
                headers=headers,
                params=params,
                timeout=self.settings.market_data_timeout
            )
            response.raise_for_status()
            data = response.json()
            
            if "data" in data and symbol.upper() in data["data"]:
                crypto_data = data["data"][symbol.upper()][0]
                quote = crypto_data.get("quote", {}).get("EUR", {})
                price = quote.get("price")
                
                if price and price > 0:
                    return {
                        "name": crypto_data.get("name"),
                        "currency": "EUR",
                        "price": Decimal(str(price)),
                        "symbol": symbol.upper()
                    }
            
            return None
        except Exception:
            return None

    def search(self, query: str) -> List[Dict]:
        if not query or not query.strip() or not self.api_key:
            return []
            
        try:
            headers = {
                "X-CMC_PRO_API_KEY": self.api_key,
                "Accept": "application/json"
            }
            
            params = {
                "symbol": query.upper(),
                "limit": 15
            }
            
            response = requests.get(
                f"{self.api_url}/v1/cryptocurrency/map",
                headers=headers,
                params=params,
                timeout=self.settings.market_data_timeout
            )
            response.raise_for_status()
            data = response.json()
            
            results = []
            if "data" in data:
                for item in data["data"]:
                    if query.lower() in item.get("symbol", "").lower() or query.lower() in item.get("name", "").lower():
                        results.append({
                            "symbol": item.get("symbol"),
                            "name": item.get("name"),
                            "exchange": None,
                            "type": "CRYPTO",
                            "currency": None
                        })
                        if len(results) >= 15:
                            break
            
            return results
        except Exception:
            return []

    def get_bulk_info(self, symbols: List[str]) -> Dict[str, Dict]:
        if not symbols or not self.api_key:
            return {}
        
        valid_symbols = [s.strip().upper() for s in symbols if s and s.strip()]
        if not valid_symbols:
            return {}
        
        results = {}
        try:
            headers = {
                "X-CMC_PRO_API_KEY": self.api_key,
                "Accept": "application/json"
            }
            
            params = {
                "symbol": ",".join(valid_symbols),
                "convert": "EUR"
            }
            
            response = requests.get(
                f"{self.api_url}/v2/cryptocurrency/quotes/latest",
                headers=headers,
                params=params,
                timeout=self.settings.market_data_timeout
            )
            response.raise_for_status()
            data = response.json()
            
            if "data" in data:
                for symbol in valid_symbols:
                    if symbol in data["data"]:
                        crypto_data = data["data"][symbol][0]
                        quote = crypto_data.get("quote", {}).get("EUR", {})
                        price = quote.get("price")
                        
                        if price and price > 0:
                            results[symbol] = {
                                "price": Decimal(str(price)),
                                "name": crypto_data.get("name"),
                                "currency": "EUR"
                            }
            
            return results
        except Exception:
            return {}
