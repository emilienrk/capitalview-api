import pytest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch, MagicMock, ANY
from sqlmodel import Session, select

from services.market import get_market_price, get_market_info, CACHE_DURATION
from models.market import MarketPrice
from models.enums import AssetType

@pytest.fixture
def mock_market_manager():
    with patch("services.market.market_data_manager") as mock:
        yield mock

def test_get_market_price_cache_hit(session: Session, mock_market_manager):
    """Test retrieving price from valid cache."""
    symbol = "BTC-USD"
    price = Decimal("50000.0")
    now = datetime.now(timezone.utc)
    entry = MarketPrice(
        symbol=symbol,
        name="Bitcoin",
        current_price=price,
        currency="USD",
        last_updated=now
    )
    session.add(entry)
    session.commit()
    result = get_market_price(session, symbol)
    assert result == price
    mock_market_manager.get_info.assert_not_called()

def test_get_market_price_no_cache(session: Session, mock_market_manager):
    """Test fetching price when no cache exists."""
    symbol = "ETH-USD"
    mock_data = {"name": "Ethereum", "price": Decimal("3000.0"), "currency": "USD"}
    mock_market_manager.get_info.return_value = mock_data
    result = get_market_price(session, symbol)
    assert result == Decimal("3000.0")
    mock_market_manager.get_info.assert_called_once_with(symbol, AssetType.STOCK)
    cached = session.exec(select(MarketPrice).where(MarketPrice.symbol == symbol)).first()
    assert cached is not None
    assert cached.current_price == Decimal("3000.0")
    assert cached.name == "Ethereum"

def test_get_market_price_expired_cache(session: Session, mock_market_manager):
    """Test refreshing price when cache is expired."""
    symbol = "SOL-USD"
    old_price = Decimal("20.0")
    new_price = Decimal("25.0")
    expired_time = datetime.now(timezone.utc) - CACHE_DURATION - timedelta(minutes=1)
    entry = MarketPrice(
        symbol=symbol,
        name="Solana",
        current_price=old_price,
        currency="USD",
        last_updated=expired_time
    )
    session.add(entry)
    session.commit()
    mock_market_manager.get_info.return_value = {"name": "Solana", "price": new_price, "currency": "USD"}
    result = get_market_price(session, symbol)
    assert result == new_price
    mock_market_manager.get_info.assert_called_once_with(symbol, AssetType.STOCK)
    session.refresh(entry)
    assert entry.current_price == new_price
    updated_at = entry.last_updated
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    assert updated_at > expired_time

def test_get_market_price_fetch_fail_no_cache(session: Session, mock_market_manager):
    """Test fetch failure with no cache."""
    symbol = "UNKNOWN"
    mock_market_manager.get_info.return_value = None
    result = get_market_price(session, symbol)
    assert result is None

def test_get_market_price_fetch_fail_expired_cache(session: Session, mock_market_manager):
    symbol = "STALE-USD"
    price = Decimal("100.0")
    expired_time = datetime.now(timezone.utc) - CACHE_DURATION - timedelta(minutes=10)
    entry = MarketPrice(symbol=symbol, name="Stale Coin", current_price=price, currency="USD", last_updated=expired_time)
    session.add(entry)
    session.commit()
    mock_market_manager.get_info.return_value = None
    result = get_market_price(session, symbol)
    assert result == price
    mock_market_manager.get_info.assert_called_once()

def test_get_market_info_cache_hit(session: Session, mock_market_manager):
    symbol = "BTC"
    name = "Bitcoin"
    price = Decimal("1.0")
    entry = MarketPrice(symbol=symbol, name=name, current_price=price, last_updated=datetime.now(timezone.utc))
    session.add(entry)
    session.commit()
    n, p = get_market_info(session, symbol)
    assert n == name
    assert p == price
    mock_market_manager.get_info.assert_not_called()

def test_get_market_info_fetch_new(session: Session, mock_market_manager):
    symbol = "NEW"
    mock_market_manager.get_info.return_value = {"name": "New Coin", "price": Decimal("10.0"), "currency": "USD"}
    n, p = get_market_info(session, symbol)
    assert n == "New Coin"
    assert p == Decimal("10.0")
    cached = session.exec(select(MarketPrice).where(MarketPrice.symbol == symbol)).first()
    assert cached.name == "New Coin"

def test_get_market_info_fetch_fail_no_cache(session: Session, mock_market_manager):
    mock_market_manager.get_info.return_value = None
    n, p = get_market_info(session, "MISSING")
    assert n is None
    assert p is None

def test_get_market_info_fetch_fail_expired(session: Session, mock_market_manager):
    symbol = "EXP"
    entry = MarketPrice(symbol=symbol, name="Expired", current_price=Decimal("5.0"), last_updated=datetime.now(timezone.utc) - timedelta(days=1))
    session.add(entry)
    session.commit()
    mock_market_manager.get_info.return_value = None
    n, p = get_market_info(session, symbol)
    assert n == "Expired"
    assert p == Decimal("5.0")

def test_get_market_price_cache_hit_naive_datetime(session: Session, mock_market_manager):
    """Test retrieving price from cache with naive datetime (simulating some DB drivers)."""
    symbol = "NAIVE"
    price = Decimal("10.0")
    now_naive = datetime.now()
    entry = MarketPrice(symbol=symbol, name="Naive Coin", current_price=price, currency="USD", last_updated=now_naive)
    session.add(entry)
    session.commit()
    result = get_market_price(session, symbol)
    assert result == price
