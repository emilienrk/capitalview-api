"""Market price service - shared utilities."""

from decimal import Decimal

from sqlmodel import Session, select

from models import MarketPrice


def get_market_price(session: Session, symbol: str) -> Decimal | None:
    """Get current price for a symbol."""
    price = session.exec(
        select(MarketPrice).where(MarketPrice.symbol == symbol)
    ).first()
    return price.current_price if price else None


def get_market_info(session: Session, symbol: str) -> tuple[str | None, Decimal | None]:
    """Get name and current price for a symbol."""
    market = session.exec(
        select(MarketPrice).where(MarketPrice.symbol == symbol)
    ).first()
    if market:
        return market.name, market.current_price
    return None, None
