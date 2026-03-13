from datetime import date, timedelta
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
        return [AssetType.STOCK, AssetType.FIAT]

    def get_info(self, symbol: str, asset_type: Optional[AssetType] = None) -> Optional[Dict]:
        if not symbol or not symbol.strip():
            return None
            
        original_symbol = symbol
        if asset_type == AssetType.FIAT and not symbol.endswith("EUR=X"):
            symbol = f"{symbol}EUR=X"
            
        try:
            ticker = yf.Ticker(symbol)
            
            info = ticker.info
            if not info:
                return None
            
            price = info.get("currentPrice") or info.get("regularMarketPrice") or info.get("ask")
            
            if (not price or price <= 0) and hasattr(ticker, "fast_info"):
                try:
                    price = ticker.fast_info.last_price
                except (AttributeError, TypeError):
                    pass
            
            if not price or price <= 0:
                return None

            isin = None
            try:
                raw_isin = ticker.isin
                if raw_isin and raw_isin != "-":
                    isin = raw_isin
            except Exception:
                pass

            return {
                "name": info.get("shortName") or info.get("longName") or original_symbol,
                "currency": info.get("currency", "EUR"),
                "price": Decimal(str(price)),
                "symbol": original_symbol,
                "isin": isin
            }
        except (ValueError, IndexError, KeyError):
             return None
        except Exception as e:
            print(f"YahooProvider unexpected error for {symbol}: {e}")
            return None

    def search(self, query: str, asset_type: Optional[AssetType] = None) -> List[Dict]:
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
        except Exception as e:
            print(f"YahooProvider search error: {e}")
            return []

    def get_bulk_info(self, symbols: List[str], asset_type: Optional[AssetType] = None) -> Dict[str, Dict]:
        if not symbols:
            return {}
        
        valid_symbols = [s.strip() for s in symbols if s and s.strip()]
        if not valid_symbols:
            return {}

        original_symbols = {}
        if asset_type == AssetType.FIAT:
            for i in range(len(valid_symbols)):
                original = valid_symbols[i]
                if not original.endswith("EUR=X"):
                    valid_symbols[i] = f"{original}EUR=X"
                original_symbols[valid_symbols[i]] = original
        else:
            for sym in valid_symbols:
                original_symbols[sym] = sym
        
        results = {}
        try:
            tickers = yf.Tickers(" ".join(valid_symbols))
            
            for sym in valid_symbols:
                original_sym = original_symbols[sym]
                if hasattr(tickers, 'tickers') and sym not in tickers.tickers:
                    continue

                ticker = tickers.tickers[sym]
                
                price = None
                currency = "EUR"
                name = original_sym
                isin = None

                if hasattr(ticker, "fast_info"):
                    try:
                        last_price = ticker.fast_info.last_price
                        if last_price and last_price > 0:
                            price = last_price
                            currency = getattr(ticker.fast_info, "currency", "EUR")
                    except Exception:
                         pass
                
                if not price:
                    try:
                        info = ticker.info
                        if info:
                            price = info.get("currentPrice") or info.get("regularMarketPrice") or info.get("ask")
                            name = info.get("shortName") or info.get("longName") or name
                            currency = info.get("currency") or currency
                    except Exception:
                        pass

                # Fetch ISIN separately (always, not just in fallback path)
                if isin is None:
                    try:
                        raw_isin = ticker.isin
                        if raw_isin and raw_isin != "-":
                            isin = raw_isin
                    except Exception:
                        pass
                
                if price and price > 0:
                    results[original_sym] = {
                        "price": Decimal(str(price)),
                        "name": name,
                        "currency": currency,
                        "isin": isin
                    }
            return results
        except Exception as e:
            print(f"YahooProvider bulk error: {e}")
            return {}

    def get_historical_prices(
        self, symbol: str, from_date: date, to_date: date, asset_type: Optional[AssetType] = None
    ) -> dict[date, Decimal]:
        if not symbol or not symbol.strip():
            return {}

        original_symbol = symbol
        if asset_type == AssetType.FIAT and not symbol.endswith("EUR=X"):
            symbol = f"{symbol}EUR=X"

        try:
            ticker = yf.Ticker(symbol)
            # end is exclusive in yfinance
            hist = ticker.history(start=from_date, end=to_date + timedelta(days=1))
        except Exception as e:
            print(f"YahooProvider history error for {symbol}: {e}")
            return {}

        if hist.empty:
            return {}

        result: dict[date, Decimal] = {}
        for dt_idx, row in hist.iterrows():
            d: date = dt_idx.date() if hasattr(dt_idx, "date") else dt_idx
            close = row.get("Close")
            if close and close > 0:
                result[d] = Decimal(str(close))
        return result