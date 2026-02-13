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


def get_stock_price(session: Session, isin: str) -> Optional[Decimal]:
    """Get current market price for a Stock (lookup by ISIN)."""
    return _get_market_price_internal(session, isin, AssetType.STOCK)


def get_crypto_price(session: Session, symbol: str) -> Optional[Decimal]:
    """Get current market price for a Crypto (lookup by Symbol)."""
    return _get_market_price_internal(session, symbol, AssetType.CRYPTO)


def _get_market_price_internal(session: Session, lookup_key: str, asset_type: AssetType) -> Optional[Decimal]:
    """Shared logic for fetching price."""
    cached = session.exec(select(MarketPrice).where(MarketPrice.isin == lookup_key)).first()

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
    """Shared logic for fetching info."""
    cached = session.exec(select(MarketPrice).where(MarketPrice.isin == lookup_key)).first()

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
