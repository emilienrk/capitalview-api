import pytest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch, MagicMock, ANY
from sqlmodel import Session, select

from services.market import get_stock_price, get_stock_info, get_crypto_price, get_crypto_info, CACHE_DURATION, _upsert_price
from models.market import MarketAsset, MarketPriceHistory
from models.enums import AssetType

@pytest.fixture
def mock_market_manager():
    with patch("services.market.market_data_manager") as mock:
        yield mock


def _make_asset(session, *, isin, symbol, name, currency="USD", asset_type=None):
    """Helper: create a MarketAsset and return it."""
    ma = MarketAsset(isin=isin, symbol=symbol, name=name, currency=currency, asset_type=asset_type)
    session.add(ma)
    session.commit()
    session.refresh(ma)
    return ma


def _make_price(session, asset_id, price, *, updated_at=None):
    """Helper: insert a price row for today and optionally override updated_at."""
    _upsert_price(session, asset_id, price)
    session.commit()
    if updated_at is not None:
        entry = session.exec(
            select(MarketPriceHistory).where(
                MarketPriceHistory.market_asset_id == asset_id,
                MarketPriceHistory.price_date == date.today(),
            )
        ).first()
        entry.updated_at = updated_at
        session.add(entry)
        session.commit()


def test_get_stock_price_cache_hit(session: Session, mock_market_manager):
    """Test retrieving stock price from valid cache using ISIN."""
    isin = "US1234567890"
    price = Decimal("50000.0")
    now = datetime.now(timezone.utc)
    ma = _make_asset(session, isin=isin, symbol="BTC-USD", name="Bitcoin")
    _make_price(session, ma.id, price, updated_at=now)
    result = get_stock_price(session, isin)
    assert result == price
    mock_market_manager.get_info.assert_not_called()

def test_get_crypto_price_cache_hit(session: Session, mock_market_manager):
    """Test retrieving crypto price from valid cache using Symbol (which acts as ISIN)."""
    symbol = "BTC"
    price = Decimal("40000.0")
    now = datetime.now(timezone.utc)
    ma = _make_asset(session, isin=symbol, symbol=symbol, name="Bitcoin")
    _make_price(session, ma.id, price, updated_at=now)
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
    """Test auto-creation of MarketAsset entry when cache doesn't exist but search succeeds."""
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
    entry = session.exec(select(MarketAsset).where(MarketAsset.isin == isin)).first()
    assert entry is not None
    assert entry.symbol == "AAPL"
    assert entry.name == "Apple Inc."


def test_get_crypto_price_no_cache_auto_creates(session: Session, mock_market_manager):
    """Test auto-creation of MarketAsset entry for crypto when cache doesn't exist."""
    symbol = "SOL"
    mock_market_manager.get_info.return_value = {
        "name": "Solana", "price": Decimal("100.0"), "currency": "USD",
        "symbol": "SOL"
    }
    result = get_crypto_price(session, symbol)
    assert result == Decimal("100.0")
    entry = session.exec(select(MarketAsset).where(MarketAsset.isin == symbol)).first()
    assert entry is not None
    assert entry.symbol == "SOL"
    assert entry.name == "Solana"

def test_get_stock_price_expired_cache(session: Session, mock_market_manager):
    """Test refreshing price when cache is expired."""
    isin = "US8888888888"
    old_price = Decimal("20.0")
    new_price = Decimal("25.0")
    expired_time = datetime.now(timezone.utc) - CACHE_DURATION - timedelta(minutes=1)
    ma = _make_asset(session, isin=isin, symbol="SOL-USD", name="Solana")
    _make_price(session, ma.id, old_price, updated_at=expired_time)

    mock_market_manager.get_info.return_value = {"name": "Solana", "price": new_price, "currency": "USD"}

    result = get_stock_price(session, isin)
    assert result == new_price
    mock_market_manager.get_info.assert_called_once_with("SOL-USD", AssetType.STOCK)

def test_get_crypto_price_expired_cache(session: Session, mock_market_manager):
    """Test refreshing crypto price when cache is expired."""
    symbol = "ETH"
    old_price = Decimal("2000.0")
    new_price = Decimal("2100.0")
    expired_time = datetime.now(timezone.utc) - CACHE_DURATION - timedelta(minutes=1)
    ma = _make_asset(session, isin=symbol, symbol=symbol, name="Ethereum")
    _make_price(session, ma.id, old_price, updated_at=expired_time)

    mock_market_manager.get_info.return_value = {"name": "Ethereum", "price": new_price, "currency": "USD"}

    result = get_crypto_price(session, symbol)
    assert result == new_price
    mock_market_manager.get_info.assert_called_once_with(symbol, AssetType.CRYPTO)

def test_get_stock_price_fetch_fail_expired_cache(session: Session, mock_market_manager):
    isin = "US7777777777"
    price = Decimal("100.0")
    expired_time = datetime.now(timezone.utc) - CACHE_DURATION - timedelta(minutes=10)
    ma = _make_asset(session, isin=isin, symbol="STALE-USD", name="Stale Coin")
    _make_price(session, ma.id, price, updated_at=expired_time)

    mock_market_manager.get_info.return_value = None
    result = get_stock_price(session, isin)
    assert result == price
    mock_market_manager.get_info.assert_called_once()

def test_get_stock_info_cache_hit(session: Session, mock_market_manager):
    isin = "US6666666666"
    name = "Bitcoin"
    price = Decimal("1.0")
    now = datetime.now(timezone.utc)
    ma = _make_asset(session, isin=isin, symbol="BTC", name=name)
    _make_price(session, ma.id, price, updated_at=now)
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
    isin = "US5555555555"
    expired_time = datetime.now(timezone.utc) - timedelta(days=1)
    ma = _make_asset(session, isin=isin, symbol="EXP", name="Expired")
    _make_price(session, ma.id, Decimal("5.0"), updated_at=expired_time)
    mock_market_manager.get_info.return_value = None
    n, p = get_stock_info(session, isin)
    assert n == "Expired"
    assert p == Decimal("5.0")

def test_get_stock_price_cache_hit_naive_datetime(session: Session, mock_market_manager):
    """Test retrieving price from cache with naive datetime (simulating some DB drivers)."""
    isin = "US4444444444"
    price = Decimal("10.0")
    now_naive = datetime.now()
    ma = _make_asset(session, isin=isin, symbol="NAIVE", name="Naive Coin")
    _make_price(session, ma.id, price, updated_at=now_naive)
    result = get_stock_price(session, isin)
    assert result == price
