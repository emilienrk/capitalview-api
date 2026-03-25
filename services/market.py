"""Market data service using Provider Pattern with DB caching + daily CRON."""

import logging
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from functools import lru_cache
from typing import Optional, Tuple

import exchange_calendars as ec
import pandas as pd
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
_FALLBACK_USD_EUR = Decimal("0.92")


# ---------------------------------------------------------------------------
# Exchange calendar helpers
# ---------------------------------------------------------------------------


@lru_cache(maxsize=64)
def _get_calendar(mic: str):
    """Return an exchange_calendars calendar for a MIC code, or None if unsupported."""
    try:
        return ec.get_calendar(mic)
    except Exception:
        return None


def _is_market_open(mic: str) -> bool:
    """Return True if the exchange is currently in a trading session."""
    cal = _get_calendar(mic)
    if cal is None:
        return True  # unknown exchange → assume open; be conservative
    try:
        return bool(cal.is_open_on_minute(pd.Timestamp.now(tz="UTC")))
    except Exception:
        return True


def _last_market_close(mic: str) -> Optional[datetime]:
    """Return the UTC datetime of the most recent session close for a MIC code."""
    cal = _get_calendar(mic)
    if cal is None:
        return None
    try:
        prev_close = cal.previous_close(pd.Timestamp.now(tz="UTC"))
        return prev_close.to_pydatetime()
    except Exception:
        return None


def _is_cache_fresh(
    asset: MarketAsset,
    price_entry: MarketPriceHistory,
    _now: Optional[datetime] = None,
) -> bool:
    """
    Decide whether a cached price is fresh enough to skip an API call.

    Rules:
    - Fiat (forex, Mon–Fri 24h): stale after CACHE_DURATION on weekdays;
      always valid on weekends (Sat/Sun UTC — forex is closed, no new price available).
      Weekend boundary uses UTC because forex closes/opens at 22:00 UTC Fri/Sun.
    - Crypto (24/7, no exchange session): stale after CACHE_DURATION.
    - Stock/ETF with a known MIC calendar:
        * Market currently open  → stale after CACHE_DURATION.
        * Market currently closed → valid as long as updated_at >= last session close
          (no point calling the API when the price won't change).
    - Stock with unknown/unsupported MIC → fall back to CACHE_DURATION.

    _now is injectable for testing (defaults to datetime.now(UTC)).
    """
    updated_at = price_entry.updated_at
    if updated_at and updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    now = _now if _now is not None else datetime.now(timezone.utc)

    # Fiat: forex is closed on weekends — check BEFORE the generic no-exchange fallback
    # because fiat assets typically have no exchange MIC.
    if asset.asset_type == AssetType.FIAT:
        if now.weekday() >= 5:  # Saturday=5, Sunday=6 in UTC
            return True  # weekend: forex closed, no new prices available
        return bool(updated_at and updated_at > now - CACHE_DURATION)

    # Crypto (24/7) or asset with no exchange MIC → hourly TTL
    if asset.asset_type == AssetType.CRYPTO or not asset.exchange:
        return bool(updated_at and updated_at > now - CACHE_DURATION)

    # Stock: consult the exchange calendar via MIC code
    if _is_market_open(asset.exchange):
        return bool(updated_at and updated_at > now - CACHE_DURATION)

    # Market is closed — valid if last update was after the most recent close
    last_close = _last_market_close(asset.exchange)
    if last_close is None:
        return bool(updated_at and updated_at > now - CACHE_DURATION)
    return bool(updated_at and updated_at >= last_close)


def get_exchange_rate(
    session: Session,
    from_currency: str = "USD",
    to_currency: str = "EUR",
    db_only: bool = False,
) -> Decimal:
    """Return the exchange rate *from_currency* → *to_currency*."""
    if from_currency == to_currency:
        return Decimal("1")

    rate_from_eur = Decimal("1")
    rate_to_eur = Decimal("1")

    if from_currency != "EUR":
        _, price = _get_market_info_internal(session, from_currency, AssetType.FIAT, db_only=db_only)
        rate_from_eur = price if price is not None else (_FALLBACK_USD_EUR if from_currency == "USD" else Decimal("1"))
        
    if to_currency != "EUR":
        _, price = _get_market_info_internal(session, to_currency, AssetType.FIAT, db_only=db_only)
        rate_to_eur = price if price is not None else (_FALLBACK_USD_EUR if to_currency == "USD" else Decimal("1"))

    if rate_to_eur == Decimal("0"):
        return Decimal("1")
        
    return rate_from_eur / rate_to_eur


def _to_eur(session: Session, price: Decimal, currency: str) -> Decimal:
    """Convert *price* to EUR. Returns unchanged if already EUR."""
    if not currency or currency.upper() == "EUR":
        return price
    return price * get_exchange_rate(session, currency.upper(), "EUR")


def _ensure_asset_type(asset: MarketAsset, asset_type: AssetType) -> bool:
    """Repair legacy rows that predate the asset_type column backfill."""
    if asset.asset_type == asset_type:
        return False
    if asset.asset_type is not None:
        return False
    asset.asset_type = asset_type
    return True

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


def _get_latest_price_entry_as_of(
    session: Session,
    asset_id: int,
    as_of: date,
) -> Optional[MarketPriceHistory]:
    """Return the latest price row with price_date <= as_of."""
    return session.exec(
        select(MarketPriceHistory)
        .where(
            MarketPriceHistory.market_asset_id == asset_id,
            MarketPriceHistory.price_date <= as_of,
        )
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
        session.exec(stmt)
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
    """Fetch live data from external API, upsert today's price (in EUR), update asset metadata."""
    if not entry.symbol:
        return None

    if _ensure_asset_type(entry, asset_type):
        session.add(entry)

    data = market_data_manager.get_info(entry.symbol, asset_type)
    if data:
        entry.name = data["name"]
        if "exchange" in data:
            entry.exchange = data["exchange"]
        session.add(entry)

        eur_price = _to_eur(session, data["price"], data.get("currency", "USD"))
        _upsert_price(session, entry.id, eur_price)
        session.commit()
        # Return EUR price so all callers get a consistent EUR value.
        return {**data, "price": eur_price, "currency": "EUR"}
    return None


def get_or_create_market_asset(
    session: Session,
    lookup_key: str,
    asset_type: AssetType,
    symbol_hint: Optional[str] = None,
) -> Optional[MarketAsset]:
    """
    Public entry-point: find or auto-create a MarketAsset for a given key.

    lookup_key  — ISIN for stocks, ticker symbol for crypto/fiat.
    symbol_hint — known ticker to try first for get_info (avoids a search round-trip).
                  Useful when the caller already has the symbol (e.g. from a form field).
    """
    # Fast path: already in DB
    existing = session.exec(
        select(MarketAsset).where(MarketAsset.isin == lookup_key)
    ).first()
    if existing:
        if _ensure_asset_type(existing, asset_type):
            session.add(existing)
            session.commit()
            session.refresh(existing)
        return existing

    return _create_market_asset_entry(session, lookup_key, asset_type, symbol_hint=symbol_hint)


def _create_market_asset_entry(
    session: Session, lookup_key: str, asset_type: AssetType, symbol_hint: Optional[str] = None
) -> Optional[MarketAsset]:
    """Auto-create a MarketAsset entry (+ initial price) when it doesn't exist."""
    market_info = None

    if asset_type == AssetType.STOCK:
        # If we already know the ticker, try get_info directly before doing a search
        if symbol_hint:
            market_info = market_data_manager.get_info(symbol_hint, AssetType.STOCK)
        if not market_info:
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
    elif asset_type == AssetType.FIAT:
        market_info = market_data_manager.get_info(lookup_key, AssetType.FIAT)
        if not market_info:
            market_info = {
                "name": lookup_key,
                "symbol": lookup_key,
                "currency": "EUR",
                "price": Decimal("0"),
                "exchange": None,
            }

    if not market_info:
        return None

    price = market_info.get("price") or Decimal("0")

    name = market_info.get("name")
    if asset_type == AssetType.FIAT:
        name = lookup_key

    existing = session.exec(
        select(MarketAsset).where(MarketAsset.isin == lookup_key)
    ).first()
    if existing:
        if _ensure_asset_type(existing, asset_type):
            session.add(existing)
            session.commit()
            session.refresh(existing)
        return existing

    ma = MarketAsset(
        isin=lookup_key,
        symbol=market_info.get("symbol") or lookup_key,
        name=name,
        exchange=market_info.get("exchange"),
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
            if _ensure_asset_type(existing, asset_type):
                session.add(existing)
                session.commit()
            return existing
        return None
    session.refresh(ma)

    if price > 0:
        native_currency = market_info.get("currency", "EUR" if asset_type == AssetType.STOCK else "USD")
        eur_price = _to_eur(session, price, native_currency)
        _upsert_price(session, ma.id, eur_price)
        session.commit()

    return ma


# ---------------------------------------------------------------------------
# Public API (signatures unchanged)
# ---------------------------------------------------------------------------


def search_assets(query: str, asset_type: AssetType) -> list[dict]:
    """Search for market assets by name or symbol. Delegates to the provider manager."""
    return market_data_manager.search(query, asset_type)


def get_assets_bulk_info(session: Session, symbols: list[str], asset_type: AssetType) -> dict[str, dict]:
    """Fetch live info for multiple symbols and convert all prices to EUR."""
    data = market_data_manager.get_bulk_info(symbols, asset_type)
    for sym, info in data.items():
        if info.get("price"):
            currency = info.get("currency")
            if asset_type == AssetType.CRYPTO and not currency:
                currency = "USD"
            elif not currency:
                currency = "EUR"
            
            eur_price = _to_eur(session, info["price"], currency)
            info["price"] = eur_price
            info["currency"] = "EUR"
    return data


def get_stock_price(
    session: Session,
    isin: str,
    db_only: bool = False,
    as_of: Optional[date] = None,
) -> Optional[Decimal]:
    """Get current market price for a Stock (lookup by ISIN)."""
    _, price = _get_market_info_internal(
        session,
        isin,
        AssetType.STOCK,
        db_only=db_only,
        as_of=as_of,
    )
    return price


def get_crypto_price(
    session: Session,
    symbol: str,
    db_only: bool = False,
    as_of: Optional[date] = None,
) -> Optional[Decimal]:
    """Get current market price for a Crypto (lookup by Symbol)."""
    _, price = _get_market_info_internal(
        session,
        symbol,
        AssetType.CRYPTO,
        db_only=db_only,
        as_of=as_of,
    )
    return price


def _get_market_info_internal(
    session: Session,
    lookup_key: str,
    asset_type: AssetType,
    db_only: bool = False,
    as_of: Optional[date] = None,
) -> Tuple[Optional[str], Optional[Decimal]]:
    """Shared logic for fetching info. Auto-creates missing entries."""
    target_date = as_of or date.today()
    today = date.today()

    cached = session.exec(
        select(MarketAsset).where(MarketAsset.isin == lookup_key)
    ).first()

    if not cached:
        if db_only or target_date < today:
            # Asset unknown → nothing in DB, return empty immediately (no API call)
            return None, None
        cached = _create_market_asset_entry(session, lookup_key, asset_type)
        if not cached:
            return None, None

    if db_only:
        # Return latest cached price up to target_date (no API call).
        latest = _get_latest_price_entry_as_of(session, cached.id, target_date)
        return cached.name, (latest.price if latest else None)

    # Historical valuation mode: never call live API for past dates.
    if target_date < today:
        latest = _get_latest_price_entry_as_of(session, cached.id, target_date)
        return cached.name, (latest.price if latest else None)

    today_entry = _get_today_price(session, cached.id)
    if today_entry and _is_cache_fresh(cached, today_entry):
        return cached.name, today_entry.price

    data = _update_cache(session, cached, asset_type)
    if data:
        return data["name"], data["price"]

    latest = _get_latest_price_entry(session, cached.id)
    return cached.name, (latest.price if latest else None)


def get_stock_info(
    session: Session,
    isin: str,
    db_only: bool = False,
    as_of: Optional[date] = None,
) -> Tuple[Optional[str], Optional[Decimal]]:
    """Get (Name, Price) for a Stock."""
    return _get_market_info_internal(
        session,
        isin,
        AssetType.STOCK,
        db_only=db_only,
        as_of=as_of,
    )


def get_crypto_info(
    session: Session,
    symbol: str,
    db_only: bool = False,
    as_of: Optional[date] = None,
) -> Tuple[Optional[str], Optional[Decimal]]:
    """Get (Name, Price) for a Crypto."""
    return _get_market_info_internal(
        session,
        symbol,
        AssetType.CRYPTO,
        db_only=db_only,
        as_of=as_of,
    )


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
                MarketAsset.asset_type == AssetType.STOCK,
                MarketAsset.symbol.isnot(None),  # type: ignore[union-attr]
            )
        ).all()

        stock_symbols = [a.symbol for a in stock_assets if a.symbol]
        symbol_to_id = {a.symbol: a.id for a in stock_assets if a.symbol}

        for i in range(0, len(stock_symbols), 50):
            batch = stock_symbols[i : i + 50]
            try:
                data = yahoo.get_bulk_info(batch, AssetType.STOCK)
                for sym, info in data.items():
                    asset_id = symbol_to_id.get(sym)
                    if asset_id and info.get("price"):
                        currency = info.get("currency") or "EUR"
                        prices_collected[asset_id] = _to_eur(session, info["price"], currency)
            except Exception as exc:
                logger.error("Yahoo batch error (symbols %s): %s", batch, exc)
            if i + 50 < len(stock_symbols):
                time.sleep(2)
                
        # ── Fiats ─────────────────────────────────────────────
        fiat_assets = session.exec(
            select(MarketAsset).where(
                MarketAsset.asset_type == AssetType.FIAT,
                MarketAsset.symbol.isnot(None),  # type: ignore[union-attr]
            )
        ).all()

        fiat_symbols = [a.symbol for a in fiat_assets if a.symbol]
        fiat_symbol_to_id = {a.symbol: a.id for a in fiat_assets if a.symbol}

        for i in range(0, len(fiat_symbols), 50):
            batch = fiat_symbols[i : i + 50]
            try:
                data = yahoo.get_bulk_info(batch, AssetType.FIAT)
                for sym, info in data.items():
                    asset_id = fiat_symbol_to_id.get(sym)
                    if asset_id and info.get("price"):
                        # price is already in EUR scale
                        prices_collected[asset_id] = Decimal(str(info["price"]))
            except Exception as exc:
                logger.error("Yahoo FIAT batch error (symbols %s): %s", batch, exc)
            if i + 50 < len(fiat_symbols):
                time.sleep(2)

        # ── Cryptos ───────────────────────────────────────────
        crypto_assets = session.exec(
            select(MarketAsset).where(
                MarketAsset.asset_type == AssetType.CRYPTO,
                MarketAsset.symbol.isnot(None),  # type: ignore[union-attr]
            )
        ).all()

        crypto_symbols = list({a.symbol for a in crypto_assets if a.symbol})
        crypto_symbol_to_id = {a.symbol: a.id for a in crypto_assets if a.symbol}

        for i in range(0, len(crypto_symbols), 100):
            batch = crypto_symbols[i : i + 100]
            try:
                data = cmc.get_bulk_info(batch, AssetType.CRYPTO)
                for sym, info in data.items():
                    asset_id = crypto_symbol_to_id.get(sym)
                    if asset_id and info.get("price"):
                        # CoinMarketCap always returns USD prices
                        prices_collected[asset_id] = _to_eur(session, info["price"], "USD")
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
            session.exec(stmt)
            session.commit()

        logger.info("CRON update_all_prices_daily: updated %d prices", len(prices_collected))


# ---------------------------------------------------------------------------
# Historical backfill — fill missing daily prices for a date range
# ---------------------------------------------------------------------------

# Max lookback to prevent abuse (10 year)
_MAX_BACKFILL_DAYS = 3650


def _bulk_upsert_rows(session: Session, rows: list[dict]) -> None:
    """Bulk-upsert a list of price rows into market_price_history."""
    stmt = pg_insert(MarketPriceHistory).values(rows)
    now = datetime.now(timezone.utc)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_market_price_history_asset_date",
        set_={"price": stmt.excluded.price, "updated_at": now},
    )
    session.exec(stmt)
    session.commit()


def _existing_dates_in_range(session: Session, asset_id: int, from_date: date, to_date: date) -> set[date]:
    """Return the set of dates that already have a price row for this asset."""
    rows = session.exec(
        select(MarketPriceHistory).where(
            MarketPriceHistory.market_asset_id == asset_id,
            MarketPriceHistory.price_date >= from_date,
            MarketPriceHistory.price_date <= to_date,
        )
    ).all()
    return {r.price_date for r in rows}


def _date_range(from_date: date, to_date: date):
    current = from_date
    while current <= to_date:
        yield current
        current += timedelta(days=1)


def _get_or_create_forex_asset(session: Session, currency: str) -> MarketAsset:
    """Return (or auto-create) a FIAT MarketAsset tracking currency vs EUR."""
    asset = session.exec(select(MarketAsset).where(MarketAsset.isin == currency)).first()
    if not asset:
        asset = MarketAsset(
            isin=currency,
            symbol=currency,
            name=currency,
            asset_type=AssetType.FIAT,
        )
        session.add(asset)
        try:
            session.commit()
        except Exception:
            session.rollback()
            asset = session.exec(select(MarketAsset).where(MarketAsset.isin == currency)).first()
    return asset


def get_historical_exchange_rates_db(
    session: Session,
    currency: str,
    from_date: date,
    to_date: date,
) -> dict[date, Decimal]:
    """Return daily exchange rates currency → EUR for [from_date, to_date]."""
    if currency.upper() == "EUR":
        return {d: Decimal("1") for d in _date_range(from_date, to_date)}

    asset = _get_or_create_forex_asset(session, currency)
    existing = _existing_dates_in_range(session, asset.id, from_date, to_date)

    stored_rows = session.exec(
        select(MarketPriceHistory).where(
            MarketPriceHistory.market_asset_id == asset.id,
            MarketPriceHistory.price_date >= from_date,
            MarketPriceHistory.price_date <= to_date,
        )
    ).all()
    result: dict[date, Decimal] = {r.price_date: r.price for r in stored_rows}

    if len(existing) < (to_date - from_date).days + 1:
        fetched = market_data_manager.get_historical_prices(currency, AssetType.FIAT, from_date, to_date)
        if fetched:
            now = datetime.now(timezone.utc)
            new_rows = [
                {
                    "market_asset_id": asset.id,
                    "price": rate,
                    "price_date": d,
                    "created_at": now,
                    "updated_at": now,
                }
                for d, rate in fetched.items()
                if d not in existing
            ]
            if new_rows:
                _bulk_upsert_rows(session, new_rows)
            result.update(fetched)

    fallback = get_exchange_rate(session, currency, "EUR")
    for d in _date_range(from_date, to_date):
        if d not in result:
            result[d] = fallback

    return result


def _backfill_stock_prices(
    session: Session, asset: MarketAsset, from_date: date, to_date: date
) -> tuple[int, int]:
    """
    Fetch daily closing prices from an api for [from_date, to_date]
    and insert the ones that are missing in the DB.
    """
    if not asset.symbol:
        return 0, 0

    existing_dates = _existing_dates_in_range(session, asset.id, from_date, to_date)
    prices = market_data_manager.get_historical_prices(
        asset.symbol, AssetType.STOCK, from_date, to_date
    )

    if not prices:
        return 0, 0

    info = market_data_manager.get_info(asset.symbol, AssetType.STOCK)
    currency = (info.get("currency") if info else None) or "EUR"

    if currency.upper() == "EUR":
        rate_by_date: dict[date, Decimal] = {}
        fallback_rate = Decimal("1")
    else:
        rate_by_date = get_historical_exchange_rates_db(session, currency, from_date, to_date)
        fallback_rate = get_exchange_rate(session, currency, "EUR")

    now = datetime.now(timezone.utc)
    rows = [
        {
            "market_asset_id": asset.id,
            "price": price * rate_by_date.get(d, fallback_rate),
            "price_date": d,
            "created_at": now,
            "updated_at": now,
        }
        for d, price in prices.items()
        if d not in existing_dates
    ]
    skipped = sum(1 for d in prices if d in existing_dates)

    if rows:
        _bulk_upsert_rows(session, rows)

    return len(rows), skipped


def _backfill_crypto_prices(
    session: Session, asset: MarketAsset, from_date: date, to_date: date
) -> tuple[int, int]:
    """
    Fetch daily closing prices from CoinGecko for [from_date, to_date]
    via the provider pattern and insert the missing ones.
    Returns (inserted, skipped).
    """
    if not asset.symbol:
        return 0, 0

    existing_dates = _existing_dates_in_range(session, asset.id, from_date, to_date)
    prices = market_data_manager.get_historical_prices(
        asset.symbol, AssetType.CRYPTO, from_date, to_date
    )

    if not prices:
        return 0, 0

    usd_eur_by_date = get_historical_exchange_rates_db(session, "USD", from_date, to_date)
    fallback_usd_eur = get_exchange_rate(session, "USD", "EUR")

    now = datetime.now(timezone.utc)
    rows = [
        {
            "market_asset_id": asset.id,
            "price": price * usd_eur_by_date.get(d, fallback_usd_eur),
            "price_date": d,
            "created_at": now,
            "updated_at": now,
        }
        for d, price in prices.items()
        if d not in existing_dates
    ]
    skipped = sum(1 for d in prices if d in existing_dates)

    if rows:
        _bulk_upsert_rows(session, rows)

    return len(rows), skipped


def backfill_price_history(
    session: Session,
    lookup_key: str,
    asset_type: AssetType,
    from_date: date,
) -> dict:
    """
    Backfill missing daily prices for an asset from `from_date` to today.
    Auto-creates the MarketAsset entry if it doesn't exist yet.
    """

    today = date.today()

    if from_date > today:
        raise ValueError("from_date ne peut pas être dans le futur")

    min_allowed = today - timedelta(days=_MAX_BACKFILL_DAYS)
    if from_date < min_allowed:
        raise ValueError(
            f"La date de départ ne peut pas dépasser {_MAX_BACKFILL_DAYS} jours dans le passé"
        )

    # Resolve or auto-create the MarketAsset
    asset = session.exec(
        select(MarketAsset).where(MarketAsset.isin == lookup_key)
    ).first()
    if not asset:
        asset = _create_market_asset_entry(session, lookup_key, asset_type)
        if not asset:
            raise ValueError(
                f"Impossible de trouver ou créer un actif pour lookup_key={lookup_key!r}"
            )

    if _ensure_asset_type(asset, asset_type):
        session.add(asset)
        session.commit()

    if asset_type == AssetType.STOCK:
        inserted, skipped = _backfill_stock_prices(session, asset, from_date, today)
    elif asset_type == AssetType.CRYPTO:
        inserted, skipped = _backfill_crypto_prices(session, asset, from_date, today)
    else:
        raise ValueError(f"Type d'actif non supporté pour le backfill : {asset_type}")

    return {
        "inserted": inserted,
        "skipped": skipped,
        "from_date": from_date,
        "to_date": today,
        "symbol": asset.symbol,
        "name": asset.name,
    }


def ensure_price_history(
    session: Session,
    lookup_key: str,
    asset_type: AssetType,
    from_date: date,
) -> None:
    """
    Guarantee that market_price_history contains data for *lookup_key* from
    *from_date* to yesterday.  Auto-creates the MarketAsset if needed.
    """

    today = date.today()
    # Clamp: never try to backfill the future
    if from_date >= today:
        return
    try:
        backfill_price_history(session, lookup_key, asset_type, from_date)
    except Exception as exc:
        logger.warning(
            "ensure_price_history: could not backfill %s (%s) from %s: %s",
            lookup_key, asset_type, from_date, exc,
        )
