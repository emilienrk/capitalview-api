"""Market data service using Provider Pattern with DB caching."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional, Tuple

from sqlmodel import Session, select

from models.market import MarketPrice
from models.enums import AssetType
from services.market_data import market_data_manager

CACHE_DURATION = timedelta(hours=1)


def _update_cache(session: Session, entry: MarketPrice, asset_type: AssetType) -> Optional[dict]:
    """Internal helper to update a MarketPrice entry from external API."""
    if not entry.symbol:
        return None
        
    data = market_data_manager.get_info(entry.symbol, asset_type)
    if data:
        now = datetime.now(timezone.utc)
        entry.current_price = data["price"]
        entry.name = data["name"]
        entry.currency = data["currency"]
        if "exchange" in data:
            entry.exchange = data["exchange"]
        entry.last_updated = now
        session.add(entry)
        session.commit()
        return data
    return None


def _create_market_price_entry(
    session: Session, lookup_key: str, asset_type: AssetType
) -> Optional[MarketPrice]:
    """
    Auto-create a MarketPrice entry when it doesn't exist in the DB.

    For stocks: search by ISIN to find the symbol, then fetch info.
    For crypto: fetch info directly using the symbol.
    """
    market_info = None

    if asset_type == AssetType.STOCK:
        # Search by ISIN to discover the symbol, then fetch live info
        results = market_data_manager.search(lookup_key, AssetType.STOCK)
        if results:
            res = results[0]
            symbol = res.get("symbol")
            if symbol:
                market_info = market_data_manager.get_info(symbol, AssetType.STOCK)
                if not market_info:
                    # Fallback: use search result metadata
                    market_info = {
                        "name": res.get("name"),
                        "symbol": symbol,
                        "currency": res.get("currency", "EUR"),
                        "price": Decimal("0"),
                        "exchange": res.get("exchange"),
                    }
    elif asset_type == AssetType.CRYPTO:
        # Fetch info directly using the symbol
        market_info = market_data_manager.get_info(lookup_key, AssetType.CRYPTO)
        if not market_info:
            # Fallback search by symbol name
            results = market_data_manager.search(lookup_key, AssetType.CRYPTO)
            if results:
                res = results[0]
                market_info = market_data_manager.get_info(res.get("symbol", lookup_key), AssetType.CRYPTO)

    if not market_info:
        return None

    now = datetime.now(timezone.utc)
    price = market_info.get("price") or Decimal("0")
    # If we got a valid price, set last_updated to now; otherwise force stale
    last_updated = now if price > 0 else datetime(2000, 1, 1, tzinfo=timezone.utc)

    mp = MarketPrice(
        isin=lookup_key,
        symbol=market_info.get("symbol") or lookup_key,
        name=market_info.get("name"),
        exchange=market_info.get("exchange"),
        current_price=price,
        currency=market_info.get("currency", "EUR" if asset_type == AssetType.STOCK else "USD"),
        last_updated=last_updated,
    )
    session.add(mp)
    try:
        session.commit()
    except Exception:
        session.rollback()
        # Another request likely inserted the same entry concurrently
        existing = session.exec(select(MarketPrice).where(MarketPrice.isin == lookup_key)).first()
        if existing:
            return existing
        return None
    session.refresh(mp)
    return mp


def get_stock_price(session: Session, isin: str) -> Optional[Decimal]:
    """Get current market price for a Stock (lookup by ISIN)."""
    return _get_market_price_internal(session, isin, AssetType.STOCK)


def get_crypto_price(session: Session, symbol: str) -> Optional[Decimal]:
    """Get current market price for a Crypto (lookup by Symbol)."""
    return _get_market_price_internal(session, symbol, AssetType.CRYPTO)


def _get_market_price_internal(session: Session, lookup_key: str, asset_type: AssetType) -> Optional[Decimal]:
    """Shared logic for fetching price. Auto-creates missing entries."""
    cached = session.exec(select(MarketPrice).where(MarketPrice.isin == lookup_key)).first()

    if not cached:
        # Entry doesn't exist — search and create it automatically
        cached = _create_market_price_entry(session, lookup_key, asset_type)
        if not cached:
            return None

    now = datetime.now(timezone.utc)
    last_updated = cached.last_updated
    if last_updated.tzinfo is None:
        last_updated = last_updated.replace(tzinfo=timezone.utc)

    if last_updated > (now - CACHE_DURATION):
        return cached.current_price

    data = _update_cache(session, cached, asset_type)
    if data:
        return data["price"]

    return cached.current_price


def get_stock_info(session: Session, isin: str) -> Tuple[Optional[str], Optional[Decimal]]:
    """Get (Name, Price) for a Stock."""
    return _get_market_info_internal(session, isin, AssetType.STOCK)


def get_crypto_info(session: Session, symbol: str) -> Tuple[Optional[str], Optional[Decimal]]:
    """Get (Name, Price) for a Crypto."""
    return _get_market_info_internal(session, symbol, AssetType.CRYPTO)


def _get_market_info_internal(session: Session, lookup_key: str, asset_type: AssetType) -> Tuple[Optional[str], Optional[Decimal]]:
    """Shared logic for fetching info. Auto-creates missing entries."""
    cached = session.exec(select(MarketPrice).where(MarketPrice.isin == lookup_key)).first()

    if not cached:
        # Entry doesn't exist — search and create it automatically
        cached = _create_market_price_entry(session, lookup_key, asset_type)
        if not cached:
            return None, None

    now = datetime.now(timezone.utc)
    last_updated = cached.last_updated
    if last_updated.tzinfo is None:
        last_updated = last_updated.replace(tzinfo=timezone.utc)

    if last_updated > (now - CACHE_DURATION):
        return cached.name, cached.current_price

    data = _update_cache(session, cached, asset_type)
    if data:
        return data["name"], data["price"]

    return cached.name, cached.current_price
