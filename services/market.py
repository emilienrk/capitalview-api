"""Market data service using Provider Pattern with DB caching."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional, Tuple

from sqlmodel import Session, select

from models.market import MarketPrice
from services.market_data import market_data_manager

# Cache validity duration
CACHE_DURATION = timedelta(hours=1)


def get_market_price(session: Session, symbol: str) -> Optional[Decimal]:
    """
    Get current market price for a symbol.
    Uses DB cache if recent, otherwise fetches via MarketDataManager.
    """
    # 1. Check Cache
    cached = session.exec(
        select(MarketPrice).where(MarketPrice.symbol == symbol)
    ).first()

    now = datetime.now(timezone.utc)

    if cached and cached.last_updated > (now - CACHE_DURATION):
        return cached.current_price

    # 2. Fetch from Manager (which handles providers)
    data = market_data_manager.get_info(symbol)
    if not data:
        return cached.current_price if cached else None

    # 3. Update/Create Cache
    if cached:
        cached.current_price = data["price"]
        cached.name = data["name"]
        cached.currency = data["currency"]
        cached.last_updated = now
        session.add(cached)
    else:
        new_entry = MarketPrice(
            symbol=symbol,
            name=data["name"],
            current_price=data["price"],
            currency=data["currency"],
            last_updated=now
        )
        session.add(new_entry)
    
    session.commit()
    
    return data["price"]


def get_market_info(session: Session, symbol: str) -> Tuple[Optional[str], Optional[Decimal]]:
    """
    Get (Name, Price) tuple for a symbol.
    """
    cached = session.exec(
        select(MarketPrice).where(MarketPrice.symbol == symbol)
    ).first()

    now = datetime.now(timezone.utc)

    if cached and cached.last_updated > (now - CACHE_DURATION):
        return cached.name, cached.current_price

    data = market_data_manager.get_info(symbol)
    if not data:
        if cached:
            return cached.name, cached.current_price
        return None, None

    if cached:
        cached.current_price = data["price"]
        cached.name = data["name"]
        cached.currency = data["currency"]
        cached.last_updated = now
        session.add(cached)
    else:
        new_entry = MarketPrice(
            symbol=symbol,
            name=data["name"],
            current_price=data["price"],
            currency=data["currency"],
            last_updated=now
        )
        session.add(new_entry)
    
    session.commit()
    
    return data["name"], data["price"]
