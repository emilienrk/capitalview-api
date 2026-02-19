from decimal import Decimal
from typing import Optional, Dict, List
from datetime import datetime, timedelta

from models.enums import AssetType
import requests

from config import get_settings
from .base import MarketDataProvider


class CoinMarketCapProvider(MarketDataProvider):
    def __init__(self):
        self.settings = get_settings()
        self.api_url = self.settings.cmc_api_url
        self.api_key = self.settings.cmc_api_key
        
        self._map_cache: List[Dict] = []
        self._map_cache_expiry: Optional[datetime] = None

    def type_assets(self) -> List[AssetType]:
        return [AssetType.CRYPTO]

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
                "convert": "USD"
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
                quote = crypto_data.get("quote", {}).get("USD", {})
                price = quote.get("price")
                
                if price and price > 0:
                    return {
                        "name": crypto_data.get("name"),
                        "currency": "USD",
                        "price": Decimal(str(price)),
                        "symbol": symbol.upper()
                    }
            
            return None
        except requests.RequestException:
            return None
        except Exception as e:
            print(f"CMC get_info error: {e}")
            return None

    def _get_map(self) -> List[Dict]:
        """Fetch and cache the crypto map (top 2000 by rank)."""
        if not self.api_key:
            return []
            
        now = datetime.now()
        if self._map_cache and self._map_cache_expiry and now < self._map_cache_expiry:
            return self._map_cache
            
        try:
            headers = {
                "X-CMC_PRO_API_KEY": self.api_key,
                "Accept": "application/json"
            }
            
            params = {
                "limit": 2000,
                "sort": "cmc_rank"
            }
            
            response = requests.get(
                f"{self.api_url}/v1/cryptocurrency/map",
                headers=headers,
                params=params,
                timeout=self.settings.market_data_timeout
            )
            response.raise_for_status()
            data = response.json()
            
            new_cache = []
            if "data" in data:
                for item in data["data"]:
                    new_cache.append({
                        "symbol": item.get("symbol"),
                        "name": item.get("name"),
                        "rank": item.get("rank"),
                        "exchange": None,
                        "type": "CRYPTO",
                        "currency": None
                    })
            
            self._map_cache = new_cache
            self._map_cache_expiry = now + timedelta(hours=24)
            return self._map_cache
            
        except Exception as e:
            print(f"Error fetching CMC map (Parsing): {e}")
            return self._map_cache

    def search(self, query: str) -> List[Dict]:
        if not query or not query.strip():
            return []
            
        all_coins = self._get_map()
        
        query_lower = query.lower()
        results = []
        
        for coin in all_coins:
            if query_lower in coin["symbol"].lower() or query_lower in coin["name"].lower():
                results.append(coin)
                
            if len(results) >= 20:
                break
                
        return results

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
                "convert": "USD"
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
                        quote = crypto_data.get("quote", {}).get("USD", {})
                        price = quote.get("price")
                        
                        if price and price > 0:
                            results[symbol] = {
                                "price": Decimal(str(price)),
                                "name": crypto_data.get("name"),
                                "currency": "USD"
                            }
            
            return results
        except Exception:
            return {}
