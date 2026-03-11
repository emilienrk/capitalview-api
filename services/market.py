"""Market data service using Provider Pattern with DB caching + daily CRON."""

import logging
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional, Tuple

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import Session, select

from database import get_engine
from models.enums import AssetType
from models.market import MarketAsset, MarketPriceHistory
from services.market_data import market_data_manager
from services.market_data.providers.coinmarketcap import CoinMarketCapProvider
from services.market_data.providers.yahoo import YahooProvider

logger = logging.getLogger(__name__)

CACHE_DURATION = timedelta(hours=1)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_today_price(session: Session, asset_id: int) -> Optional[MarketPriceHistory]:
    """Return today's price row for an asset (if it exists)."""
    today = date.today()
    return session.exec(
        select(MarketPriceHistory).where(
            MarketPriceHistory.market_asset_id == asset_id,
            MarketPriceHistory.price_date == today,
        )
    ).first()


def _get_latest_price_entry(session: Session, asset_id: int) -> Optional[MarketPriceHistory]:
    """Return the most recent price row regardless of date."""
    return session.exec(
        select(MarketPriceHistory)
        .where(MarketPriceHistory.market_asset_id == asset_id)
        .order_by(MarketPriceHistory.price_date.desc())
    ).first()


def _upsert_price(session: Session, asset_id: int, price: Decimal) -> None:
    """Insert or update today's price for an asset."""
    today = date.today()
    now = datetime.now(timezone.utc)

    dialect = session.bind.dialect.name if session.bind else "postgresql"

    if dialect == "postgresql":
        stmt = pg_insert(MarketPriceHistory).values(
            market_asset_id=asset_id,
            price=price,
            price_date=today,
            created_at=now,
            updated_at=now,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_market_price_history_asset_date",
            set_={"price": stmt.excluded.price, "updated_at": now},
        )
        session.execute(stmt)
    else:
        # SQLite / generic fallback: manual check-then-insert/update
        existing = session.exec(
            select(MarketPriceHistory).where(
                MarketPriceHistory.market_asset_id == asset_id,
                MarketPriceHistory.price_date == today,
            )
        ).first()
        if existing:
            existing.price = price
            existing.updated_at = now
            session.add(existing)
        else:
            entry = MarketPriceHistory(
                market_asset_id=asset_id,
                price=price,
                price_date=today,
                created_at=now,
                updated_at=now,
            )
            session.add(entry)


def get_latest_price(session: Session, asset_id: int) -> Optional[Decimal]:
    """Public helper: return the most recent price for a MarketAsset id."""
    entry = _get_latest_price_entry(session, asset_id)
    return entry.price if entry else None


# ---------------------------------------------------------------------------
# Cache / fetch logic
# ---------------------------------------------------------------------------


def _update_cache(session: Session, entry: MarketAsset, asset_type: AssetType) -> Optional[dict]:
    """Fetch live data from external API, upsert today's price, update asset metadata."""
    if not entry.symbol:
        return None

    data = market_data_manager.get_info(entry.symbol, asset_type)
    if data:
        entry.name = data["name"]
        entry.currency = data["currency"]
        if "exchange" in data:
            entry.exchange = data["exchange"]
        session.add(entry)

        _upsert_price(session, entry.id, data["price"])
        session.commit()
        return data
    return None


def _create_market_asset_entry(
    session: Session, lookup_key: str, asset_type: AssetType
) -> Optional[MarketAsset]:
    """Auto-create a MarketAsset entry (+ initial price) when it doesn't exist."""
    market_info = None

    if asset_type == AssetType.STOCK:
        results = market_data_manager.search(lookup_key, AssetType.STOCK)
        if results:
            res = results[0]
            symbol = res.get("symbol")
            if symbol:
                market_info = market_data_manager.get_info(symbol, AssetType.STOCK)
                if not market_info:
                    market_info = {
                        "name": res.get("name"),
                        "symbol": symbol,
                        "currency": res.get("currency", "EUR"),
                        "price": Decimal("0"),
                        "exchange": res.get("exchange"),
                    }
    elif asset_type == AssetType.CRYPTO:
        market_info = market_data_manager.get_info(lookup_key, AssetType.CRYPTO)
        if not market_info:
            results = market_data_manager.search(lookup_key, AssetType.CRYPTO)
            if results:
                res = results[0]
                market_info = market_data_manager.get_info(
                    res.get("symbol", lookup_key), AssetType.CRYPTO
                )

    if not market_info:
        return None

    price = market_info.get("price") or Decimal("0")

    ma = MarketAsset(
        isin=lookup_key,
        symbol=market_info.get("symbol") or lookup_key,
        name=market_info.get("name"),
        exchange=market_info.get("exchange"),
        currency=market_info.get("currency", "EUR" if asset_type == AssetType.STOCK else "USD"),
        asset_type=asset_type.value,
    )
    session.add(ma)
    try:
        session.commit()
    except Exception:
        session.rollback()
        existing = session.exec(
            select(MarketAsset).where(MarketAsset.isin == lookup_key)
        ).first()
        if existing:
            return existing
        return None
    session.refresh(ma)

    if price > 0:
        _upsert_price(session, ma.id, price)
        session.commit()

    return ma


# ---------------------------------------------------------------------------
# Public API (signatures unchanged)
# ---------------------------------------------------------------------------


def get_stock_price(session: Session, isin: str) -> Optional[Decimal]:
    """Get current market price for a Stock (lookup by ISIN)."""
    return _get_market_price_internal(session, isin, AssetType.STOCK)


def get_crypto_price(session: Session, symbol: str) -> Optional[Decimal]:
    """Get current market price for a Crypto (lookup by Symbol)."""
    return _get_market_price_internal(session, symbol, AssetType.CRYPTO)


def _get_market_price_internal(
    session: Session, lookup_key: str, asset_type: AssetType
) -> Optional[Decimal]:
    """Shared logic for fetching price. Auto-creates missing entries."""
    cached = session.exec(
        select(MarketAsset).where(MarketAsset.isin == lookup_key)
    ).first()

    if not cached:
        cached = _create_market_asset_entry(session, lookup_key, asset_type)
        if not cached:
            return None

    # Check today's price row
    today_entry = _get_today_price(session, cached.id)
    if today_entry:
        updated_at = today_entry.updated_at
        if updated_at and updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        if updated_at and updated_at > (now - CACHE_DURATION):
            return today_entry.price

    # Data is stale or missing for today — refresh
    data = _update_cache(session, cached, asset_type)
    if data:
        return data["price"]

    # Fallback: latest historical price
    latest = _get_latest_price_entry(session, cached.id)
    return latest.price if latest else None


def get_stock_info(session: Session, isin: str) -> Tuple[Optional[str], Optional[Decimal]]:
    """Get (Name, Price) for a Stock."""
    return _get_market_info_internal(session, isin, AssetType.STOCK)


def get_crypto_info(session: Session, symbol: str) -> Tuple[Optional[str], Optional[Decimal]]:
    """Get (Name, Price) for a Crypto."""
    return _get_market_info_internal(session, symbol, AssetType.CRYPTO)


def _get_market_info_internal(
    session: Session, lookup_key: str, asset_type: AssetType
) -> Tuple[Optional[str], Optional[Decimal]]:
    """Shared logic for fetching info. Auto-creates missing entries."""
    cached = session.exec(
        select(MarketAsset).where(MarketAsset.isin == lookup_key)
    ).first()

    if not cached:
        cached = _create_market_asset_entry(session, lookup_key, asset_type)
        if not cached:
            return None, None

    today_entry = _get_today_price(session, cached.id)
    if today_entry:
        updated_at = today_entry.updated_at
        if updated_at and updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        if updated_at and updated_at > (now - CACHE_DURATION):
            return cached.name, today_entry.price

    data = _update_cache(session, cached, asset_type)
    if data:
        return data["name"], data["price"]

    latest = _get_latest_price_entry(session, cached.id)
    return cached.name, (latest.price if latest else None)


# ---------------------------------------------------------------------------
# CRON — Daily bulk price update (called by APScheduler at 23:30)
# ---------------------------------------------------------------------------


def update_all_prices_daily() -> None:
    """
    Single entry-point for the nightly CRON job.

    * Stocks  — Yahoo Finance, batches of 50, 2 s sleep
    * Cryptos — CoinMarketCap, batches of 100, 3 s sleep
    * Bulk upsert into market_price_history (one price per asset per day)
    """
    engine = get_engine()
    yahoo = YahooProvider()
    cmc = CoinMarketCapProvider()

    prices_collected: dict[int, Decimal] = {}

    with Session(engine) as session:
        # ── Stocks ────────────────────────────────────────────
        stock_assets = session.exec(
            select(MarketAsset).where(
                MarketAsset.asset_type == AssetType.STOCK.value,
                MarketAsset.symbol.isnot(None),  # type: ignore[union-attr]
            )
        ).all()

        stock_symbols = [a.symbol for a in stock_assets if a.symbol]
        symbol_to_id = {a.symbol: a.id for a in stock_assets if a.symbol}

        for i in range(0, len(stock_symbols), 50):
            batch = stock_symbols[i : i + 50]
            try:
                data = yahoo.get_bulk_info(batch)
                for sym, info in data.items():
                    asset_id = symbol_to_id.get(sym)
                    if asset_id and info.get("price"):
                        prices_collected[asset_id] = info["price"]
            except Exception as exc:
                logger.error("Yahoo batch error (symbols %s): %s", batch, exc)
            if i + 50 < len(stock_symbols):
                time.sleep(2)

        # ── Cryptos ───────────────────────────────────────────
        crypto_assets = session.exec(
            select(MarketAsset).where(
                MarketAsset.asset_type == AssetType.CRYPTO.value,
                MarketAsset.symbol.isnot(None),  # type: ignore[union-attr]
            )
        ).all()

        crypto_symbols = list({a.symbol for a in crypto_assets if a.symbol})
        crypto_symbol_to_id = {a.symbol: a.id for a in crypto_assets if a.symbol}

        for i in range(0, len(crypto_symbols), 100):
            batch = crypto_symbols[i : i + 100]
            try:
                data = cmc.get_bulk_info(batch)
                for sym, info in data.items():
                    asset_id = crypto_symbol_to_id.get(sym)
                    if asset_id and info.get("price"):
                        prices_collected[asset_id] = info["price"]
            except Exception as exc:
                logger.error("CMC batch error (symbols %s): %s", batch, exc)
            if i + 100 < len(crypto_symbols):
                time.sleep(3)

        # ── Bulk upsert ──────────────────────────────────────
        if prices_collected:
            today = date.today()
            now = datetime.now(timezone.utc)
            rows = [
                {
                    "market_asset_id": asset_id,
                    "price": price,
                    "price_date": today,
                    "created_at": now,
                    "updated_at": now,
                }
                for asset_id, price in prices_collected.items()
            ]
            stmt = pg_insert(MarketPriceHistory).values(rows)
            stmt = stmt.on_conflict_do_update(
                constraint="uq_market_price_history_asset_date",
                set_={"price": stmt.excluded.price, "updated_at": now},
            )
            session.execute(stmt)
            session.commit()

        logger.info("CRON update_all_prices_daily: updated %d prices", len(prices_collected))
