"""CoinGecko market data provider — current prices + historical daily prices."""

import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import requests

from config import get_settings
from models.enums import AssetType

from .base import MarketDataProvider

# Free-tier CoinGecko: ~30 req/min → wait 1.2 s between calls by default
_RATE_LIMIT_SLEEP = 1.2

# In-memory symbol→id cache shared across instances (module-level)
_SYMBOL_ID_CACHE: dict[str, str] = {}


class CoinGeckoProvider(MarketDataProvider):
    """
    CoinGecko provider for crypto assets.
    Handles:
      - current price (get_info / get_bulk_info)
      - historical daily prices (get_historical_prices)
      - search by name or symbol
    """

    def __init__(self) -> None:
        self.settings = get_settings()
        self._base = self.settings.coingecko_api_url

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {"Accept": "application/json"}
        if self.settings.coingecko_api_key:
            h["x-cg-demo-apikey"] = self.settings.coingecko_api_key
        return h

    def _get(self, path: str, params: dict | None = None) -> dict | None:
        try:
            resp = requests.get(
                f"{self._base}{path}",
                params=params,
                headers=self._headers(),
                timeout=self.settings.market_data_timeout,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as exc:
            print(f"CoinGeckoProvider HTTP error {exc.response.status_code} on {path}")
            return None
        except Exception as exc:
            print(f"CoinGeckoProvider error on {path}: {exc}")
            return None

    def _resolve_id(self, symbol: str) -> str | None:
        """Resolve a ticker symbol (e.g. 'BTC') to a CoinGecko coin ID (e.g. 'bitcoin')."""
        upper = symbol.upper()
        if upper in _SYMBOL_ID_CACHE:
            return _SYMBOL_ID_CACHE[upper]

        data = self._get("/search", {"query": symbol})
        if not data:
            return None

        for coin in data.get("coins", []):
            if coin.get("symbol", "").upper() == upper:
                coin_id: str = coin["id"]
                _SYMBOL_ID_CACHE[upper] = coin_id
                return coin_id
        return None

    # ------------------------------------------------------------------
    # MarketDataProvider interface
    # ------------------------------------------------------------------

    def type_assets(self) -> list[AssetType]:
        return [AssetType.CRYPTO]

    def get_info(self, symbol: str, asset_type: AssetType | None = None) -> dict | None:
        if not symbol or not symbol.strip():
            return None

        upper = symbol.strip().upper()
        data = self._get(
            "/simple/price",
            {"ids": upper.lower(), "vs_currencies": "usd", "include_market_cap": "false"},
        )
        # Try by symbol directly (id-based lookup often requires the coingecko ID)
        # Use /coins/markets as a more reliable source
        data_markets = self._get(
            "/coins/markets",
            {
                "vs_currency": "usd",
                "ids": "",
                "symbols": upper.lower(),
                "order": "market_cap_desc",
                "per_page": 5,
                "page": 1,
            },
        )
        if data_markets and isinstance(data_markets, list):
            for coin in data_markets:
                if coin.get("symbol", "").upper() == upper:
                    price = coin.get("current_price")
                    if price and price > 0:
                        return {
                            "name": coin.get("name"),
                            "currency": "USD",
                            "price": Decimal(str(price)),
                            "symbol": upper,
                        }

        # Fallback via ID resolution
        coin_id = self._resolve_id(upper)
        if not coin_id:
            return None
        time.sleep(_RATE_LIMIT_SLEEP)

        coin_data = self._get(f"/coins/{coin_id}", {"localization": "false", "tickers": "false"})
        if not coin_data:
            return None
        price = (coin_data.get("market_data") or {}).get("current_price", {}).get("usd")
        if not price or price <= 0:
            return None
        return {
            "name": coin_data.get("name"),
            "currency": "USD",
            "price": Decimal(str(price)),
            "symbol": upper,
        }

    def search(self, query: str, asset_type: AssetType | None = None) -> list[dict]:
        if not query or not query.strip():
            return []

        data = self._get("/search", {"query": query.strip()})
        if not data:
            return []

        return [
            {
                "symbol": c.get("symbol", "").upper(),
                "name": c.get("name"),
                "exchange": None,
                "type": "CRYPTO",
                "currency": "USD",
            }
            for c in data.get("coins", [])[:20]
            if c.get("symbol")
        ]

    def get_bulk_info(self, symbols: list[str], asset_type: AssetType | None = None) -> dict[str, dict]:
        if not symbols:
            return {}

        # Resolve all symbols to CoinGecko IDs (uses cache)
        id_to_symbol: dict[str, str] = {}
        for sym in symbols:
            upper = sym.strip().upper()
            coin_id = self._resolve_id(upper)
            if coin_id:
                id_to_symbol[coin_id] = upper
            time.sleep(0.1)  # light sleep between search calls

        if not id_to_symbol:
            return {}

        # Batch price fetch — CoinGecko allows comma-separated IDs
        ids_param = ",".join(id_to_symbol.keys())
        data = self._get(
            "/simple/price",
            {"ids": ids_param, "vs_currencies": "usd"},
        )
        if not data:
            return {}

        results: dict[str, dict] = {}
        for coin_id, sym in id_to_symbol.items():
            price = data.get(coin_id, {}).get("usd")
            if price and price > 0:
                results[sym] = {
                    "price": Decimal(str(price)),
                    "currency": "USD",
                    "name": sym,
                }
        return results

    def get_historical_prices(
        self, symbol: str, from_date: date, to_date: date, asset_type: AssetType | None = None
    ) -> dict[date, Decimal]:
        """
        Fetch daily closing prices from CoinGecko for the given range.

        CoinGecko returns hourly granularity for ranges ≤90 days and daily for
        larger ranges.  We collapse to one price per calendar day (last point wins).
        A rate-limit sleep is applied before the market_chart call.
        """
        if not symbol or not symbol.strip():
            return {}

        coin_id = self._resolve_id(symbol.strip().upper())
        if not coin_id:
            return {}

        time.sleep(_RATE_LIMIT_SLEEP)

        from_ts = int(
            datetime.combine(from_date, datetime.min.time())
            .replace(tzinfo=timezone.utc)
            .timestamp()
        )
        to_ts = int(
            datetime.combine(to_date + timedelta(days=1), datetime.min.time())
            .replace(tzinfo=timezone.utc)
            .timestamp()
        )

        data = self._get(
            f"/coins/{coin_id}/market_chart/range",
            {"vs_currency": "usd", "from": from_ts, "to": to_ts},
        )
        if not data:
            return {}

        prices_by_date: dict[date, Decimal] = {}
        for ts_ms, price in data.get("prices", []):
            d = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).date()
            if from_date <= d <= to_date and price and price > 0:
                prices_by_date[d] = Decimal(str(price))  # last point of the day wins

        return prices_by_date
