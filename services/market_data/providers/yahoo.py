from decimal import Decimal
from typing import Optional, Dict, List

from models.enums import AssetType
import requests
import yfinance as yf

from config import get_settings
from .base import MarketDataProvider


class YahooProvider(MarketDataProvider):
    def __init__(self):
        self.settings = get_settings()
        self.search_url = self.settings.yahoo_api_url

    def type_assets(self) -> List[AssetType]:
        return [AssetType.STOCK]

    def get_stock(self, symbol: str) -> Optional[Decimal]:
        if not symbol or not symbol.strip():
            return None
            
        try:
            ticker = yf.Ticker(symbol)
            
            if hasattr(ticker, "fast_info"):
                try:
                    last_price = ticker.fast_info.last_price
                    if last_price and last_price > 0:
                        return Decimal(str(last_price))
                except (AttributeError, TypeError, ValueError):
                    pass
            
            data = ticker.info
            if data:
                price = data.get("currentPrice") or data.get("regularMarketPrice") or data.get("ask")
                if price and price > 0:
                    return Decimal(str(price))
            
            return None
        except Exception:
            return None

    def get_info(self, symbol: str) -> Optional[Dict]:
        if not symbol or not symbol.strip():
            return None
            
        try:
            ticker = yf.Ticker(symbol)
            info = ticker.info
            
            if not info:
                return None
            
            price = info.get("currentPrice") or info.get("regularMarketPrice") or info.get("ask")
            
            if not price or price <= 0:
                if hasattr(ticker, "fast_info"):
                    try:
                        price = ticker.fast_info.last_price
                        if not price or price <= 0:
                            return None
                    except (AttributeError, TypeError):
                        return None
                else:
                    return None

            return {
                "name": info.get("shortName") or info.get("longName") or symbol,
                "currency": info.get("currency", "EUR"),
                "price": Decimal(str(price)),
                "symbol": symbol
            }
        except Exception:
            return None

    def search(self, query: str) -> List[Dict]:
        if not query or not query.strip():
            return []
            
        try:
            params = {
                "q": query.strip(),
                "quotesCount": 15,
                "newsCount": 0,
                "enableFuzzyQuery": False,
                "quotesQueryId": "tss_match_phrase_query"
            }
            headers = {"User-Agent": self.settings.yahoo_user_agent}
            
            response = requests.get(
                self.search_url, 
                params=params, 
                headers=headers, 
                timeout=self.settings.market_data_timeout
            )
            response.raise_for_status()
            data = response.json()
            
            results = []
            if "quotes" in data and isinstance(data["quotes"], list):
                for quote in data["quotes"]:
                    if not isinstance(quote, dict) or not quote.get("isYahooFinance", True):
                        continue
                    
                    symbol = quote.get("symbol")
                    if not symbol:
                        continue
                        
                    results.append({
                        "symbol": symbol,
                        "name": quote.get("shortname") or quote.get("longname") or symbol,
                        "exchange": quote.get("exchange"),
                        "type": quote.get("quoteType"),
                        "currency": None
                    })
            return results
        except Exception:
            return []

    def get_bulk_info(self, symbols: List[str]) -> Dict[str, Dict]:
        if not symbols:
            return {}
        
        valid_symbols = [s.strip() for s in symbols if s and s.strip()]
        if not valid_symbols:
            return {}
        
        results = {}
        try:
            tickers = yf.Tickers(" ".join(valid_symbols))
            
            for sym in valid_symbols:
                try:
                    if sym not in tickers.tickers:
                        continue
                        
                    ticker = tickers.tickers[sym]
                    price = None
                    currency = "EUR"
                    name = sym
                    full_info = {}

                    if hasattr(ticker, "fast_info"):
                        try:
                            last_price = ticker.fast_info.last_price
                            if last_price and last_price > 0:
                                price = last_price
                                currency = getattr(ticker.fast_info, "currency", "EUR")
                        except (AttributeError, TypeError, ValueError):
                            pass
                    
                    if not price:
                        try:
                            full_info = ticker.info
                            if full_info:
                                price = full_info.get("currentPrice") or full_info.get("regularMarketPrice") or full_info.get("ask")
                        except Exception:
                            pass
                    
                    if not price or price <= 0:
                        continue

                    results[sym] = {
                        "price": Decimal(str(price)),
                        "name": full_info.get("shortName") or full_info.get("longName") or name,
                        "currency": full_info.get("currency") or currency
                    }
                except Exception:
                    continue
            
            return results
        except Exception:
            return {}