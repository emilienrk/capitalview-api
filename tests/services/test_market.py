import pytest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch, MagicMock, ANY
from sqlmodel import Session, select

from services.market import get_stock_price, get_stock_info, get_crypto_price, get_crypto_info, CACHE_DURATION
from models.market import MarketPrice
from models.enums import AssetType

@pytest.fixture
def mock_market_manager():
    with patch("services.market.market_data_manager") as mock:
        yield mock

def test_get_stock_price_cache_hit(session: Session, mock_market_manager):
    """Test retrieving stock price from valid cache using ISIN."""
    symbol = "BTC-USD"
    isin = "US1234567890"
    price = Decimal("50000.0")
    now = datetime.now(timezone.utc)
    entry = MarketPrice(
        isin=isin,
        symbol=symbol,
        name="Bitcoin",
        current_price=price,
        currency="USD",
        last_updated=now
    )
    session.add(entry)
    session.commit()
    result = get_stock_price(session, isin)
    assert result == price
    mock_market_manager.get_info.assert_not_called()

def test_get_crypto_price_cache_hit(session: Session, mock_market_manager):
    """Test retrieving crypto price from valid cache using Symbol (which acts as ISIN)."""
    symbol = "BTC"
    price = Decimal("40000.0")
    now = datetime.now(timezone.utc)
    # Crypto uses symbol in ISIN column
    entry = MarketPrice(
        isin=symbol,
        symbol=symbol,
        name="Bitcoin",
        current_price=price,
        currency="USD",
        last_updated=now
    )
    session.add(entry)
    session.commit()
    result = get_crypto_price(session, symbol)
    assert result == price
    mock_market_manager.get_info.assert_not_called()

def test_get_stock_price_no_cache_no_search_results(session: Session, mock_market_manager):
    """Test fetching price when no cache exists and search returns nothing."""
    isin = "US9999999999"
    mock_market_manager.search.return_value = []
    result = get_stock_price(session, isin)
    assert result is None
    mock_market_manager.search.assert_called_once_with(isin, AssetType.STOCK)


def test_get_stock_price_no_cache_auto_creates(session: Session, mock_market_manager):
    """Test auto-creation of MarketPrice entry when cache doesn't exist but search succeeds."""
    isin = "US9999999999"
    mock_market_manager.search.return_value = [
        {"symbol": "AAPL", "name": "Apple Inc.", "exchange": "NMS", "currency": "USD"}
    ]
    mock_market_manager.get_info.return_value = {
        "name": "Apple Inc.", "price": Decimal("150.0"), "currency": "USD",
        "symbol": "AAPL", "exchange": "NMS"
    }
    result = get_stock_price(session, isin)
    assert result == Decimal("150.0")
    # Verify entry was created in DB
    entry = session.exec(select(MarketPrice).where(MarketPrice.isin == isin)).first()
    assert entry is not None
    assert entry.symbol == "AAPL"
    assert entry.name == "Apple Inc."


def test_get_crypto_price_no_cache_auto_creates(session: Session, mock_market_manager):
    """Test auto-creation of MarketPrice entry for crypto when cache doesn't exist."""
    symbol = "SOL"
    mock_market_manager.get_info.return_value = {
        "name": "Solana", "price": Decimal("100.0"), "currency": "USD",
        "symbol": "SOL"
    }
    result = get_crypto_price(session, symbol)
    assert result == Decimal("100.0")
    entry = session.exec(select(MarketPrice).where(MarketPrice.isin == symbol)).first()
    assert entry is not None
    assert entry.symbol == "SOL"
    assert entry.name == "Solana"

def test_get_stock_price_expired_cache(session: Session, mock_market_manager):
    """Test refreshing price when cache is expired."""
    symbol = "SOL-USD"
    isin = "US8888888888"
    old_price = Decimal("20.0")
    new_price = Decimal("25.0")
    expired_time = datetime.now(timezone.utc) - CACHE_DURATION - timedelta(minutes=1)
    entry = MarketPrice(
        isin=isin,
        symbol=symbol,
        name="Solana",
        current_price=old_price,
        currency="USD",
        last_updated=expired_time
    )
    session.add(entry)
    session.commit()
    
    # Mock return value for the symbol update
    mock_market_manager.get_info.return_value = {"name": "Solana", "price": new_price, "currency": "USD"}
    
    result = get_stock_price(session, isin)
    assert result == new_price
    mock_market_manager.get_info.assert_called_once_with(symbol, AssetType.STOCK)
    
    session.refresh(entry)
    assert entry.current_price == new_price
    updated_at = entry.last_updated
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    assert updated_at > expired_time

def test_get_crypto_price_expired_cache(session: Session, mock_market_manager):
    """Test refreshing crypto price when cache is expired."""
    symbol = "ETH"
    old_price = Decimal("2000.0")
    new_price = Decimal("2100.0")
    expired_time = datetime.now(timezone.utc) - CACHE_DURATION - timedelta(minutes=1)
    entry = MarketPrice(
        isin=symbol,
        symbol=symbol,
        name="Ethereum",
        current_price=old_price,
        currency="USD",
        last_updated=expired_time
    )
    session.add(entry)
    session.commit()
    
    mock_market_manager.get_info.return_value = {"name": "Ethereum", "price": new_price, "currency": "USD"}
    
    result = get_crypto_price(session, symbol)
    assert result == new_price
    mock_market_manager.get_info.assert_called_once_with(symbol, AssetType.CRYPTO)

def test_get_stock_price_fetch_fail_expired_cache(session: Session, mock_market_manager):
    symbol = "STALE-USD"
    isin = "US7777777777"
    price = Decimal("100.0")
    expired_time = datetime.now(timezone.utc) - CACHE_DURATION - timedelta(minutes=10)
    entry = MarketPrice(isin=isin, symbol=symbol, name="Stale Coin", current_price=price, currency="USD", last_updated=expired_time)
    session.add(entry)
    session.commit()
    
    mock_market_manager.get_info.return_value = None
    result = get_stock_price(session, isin)
    assert result == price
    mock_market_manager.get_info.assert_called_once()

def test_get_stock_info_cache_hit(session: Session, mock_market_manager):
    symbol = "BTC"
    isin = "US6666666666"
    name = "Bitcoin"
    price = Decimal("1.0")
    entry = MarketPrice(isin=isin, symbol=symbol, name=name, current_price=price, last_updated=datetime.now(timezone.utc))
    session.add(entry)
    session.commit()
    n, p = get_stock_info(session, isin)
    assert n == name
    assert p == price
    mock_market_manager.get_info.assert_not_called()

def test_get_stock_info_fetch_fail_no_cache(session: Session, mock_market_manager):
    isin = "US_MISSING"
    mock_market_manager.search.return_value = []
    mock_market_manager.get_info.return_value = None
    n, p = get_stock_info(session, isin)
    assert n is None
    assert p is None

def test_get_stock_info_fetch_fail_expired(session: Session, mock_market_manager):
    symbol = "EXP"
    isin = "US5555555555"
    entry = MarketPrice(isin=isin, symbol=symbol, name="Expired", current_price=Decimal("5.0"), last_updated=datetime.now(timezone.utc) - timedelta(days=1))
    session.add(entry)
    session.commit()
    mock_market_manager.get_info.return_value = None
    n, p = get_stock_info(session, isin)
    assert n == "Expired"
    assert p == Decimal("5.0")

def test_get_stock_price_cache_hit_naive_datetime(session: Session, mock_market_manager):
    """Test retrieving price from cache with naive datetime (simulating some DB drivers)."""
    symbol = "NAIVE"
    isin = "US4444444444"
    price = Decimal("10.0")
    now_naive = datetime.now()
    entry = MarketPrice(isin=isin, symbol=symbol, name="Naive Coin", current_price=price, currency="USD", last_updated=now_naive)
    session.add(entry)
    session.commit()
    result = get_stock_price(session, isin)
    assert result == price
