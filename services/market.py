"""Market data service using Provider Pattern with DB caching + daily CRON."""

import bisect
import logging
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from functools import lru_cache
from typing import Callable

import exchange_calendars as ec
import pandas as pd
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import Session, select

from database import get_engine
from dtos.transaction import TransactionResponse
from models.enums import AssetType
from models.market import MarketAsset, MarketPriceHistory
from services.market_data import market_data_manager
from services.market_data.providers.coinmarketcap import CoinMarketCapProvider
from services.market_data.providers.yahoo import YahooProvider
from services.encryption import decrypt_data, hash_index
from models import StockAccount, StockTransaction, CryptoAccount, CryptoTransaction

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


def _last_market_close(mic: str) -> datetime | None:
    """Return the UTC datetime of the most recent session close for a MIC code."""
    cal = _get_calendar(mic)
    if cal is None:
        return None
    try:
        prev_close = cal.previous_close(pd.Timestamp.now(tz="UTC"))
        return prev_close.to_pydatetime()
    except Exception:
        return None


def get_non_trading_days(
    mics: list[str],
    from_date: date,
    to_date: date,
) -> list[date]:
    """
    Days in [from_date, to_date] closed on **every** given exchange (weekends +
    holidays), for stripping flat segments from stock charts.

    Union semantics: a day is a trading day if at least one held exchange has a
    session that day, so a mixed portfolio (e.g. XPAR + XNYS) only drops days
    where all its exchanges are shut. When no MIC is known/supported, returns an
    empty list — we never hide a day we are unsure about.
    """
    if to_date < from_date:
        return []

    open_dates: set[date] = set()
    queried_ok = False
    for mic in {m for m in mics if m}:
        cal = _get_calendar(mic)
        if cal is None:
            continue  # unknown/unsupported MIC → ignore, don't let it filter
        try:
            sessions = cal.sessions_in_range(pd.Timestamp(from_date), pd.Timestamp(to_date))
        except Exception:
            continue
        queried_ok = True  # successful query; an empty result means "all closed", which is valid
        for ts in sessions:
            open_dates.add(ts.date())

    # No usable calendar at all → don't filter anything (never hide unsure days).
    if not queried_ok:
        return []

    result: list[date] = []
    day = from_date
    while day <= to_date:
        if day not in open_dates:
            result.append(day)
        day += timedelta(days=1)
    return result


def _is_cache_fresh(
    asset: MarketAsset,
    price_entry: MarketPriceHistory,
    _now: datetime | None = None,
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


def has_exchange_rate(session: Session, currency: str) -> bool:
    """Whether a currency can actually be converted to euros.

    Exists because `get_exchange_rate` cannot say so: it answers 1 both for a
    rate that genuinely is 1 and for a currency it knows nothing about, and the
    caller cannot tell the two apart. Rather than change that contract at nine
    call sites, this asks the question directly, for the one place that must
    refuse rather than guess.
    """
    if not currency or currency.upper() == "EUR":
        return True
    _, price = _get_market_info_internal(session, currency.upper(), AssetType.FIAT)
    return price is not None


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


def _get_today_price(session: Session, asset_id: int) -> MarketPriceHistory | None:
    """Return today's price row for an asset (if it exists)."""
    today = date.today()
    return session.exec(
        select(MarketPriceHistory).where(
            MarketPriceHistory.market_asset_id == asset_id,
            MarketPriceHistory.price_date == today,
        )
    ).first()


def _get_latest_price_entry(session: Session, asset_id: int) -> MarketPriceHistory | None:
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
) -> MarketPriceHistory | None:
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


def get_latest_price(session: Session, asset_id: int) -> Decimal | None:
    """Public helper: return the most recent price for a MarketAsset id."""
    entry = _get_latest_price_entry(session, asset_id)
    return entry.price if entry else None


# ---------------------------------------------------------------------------
# Cache / fetch logic
# ---------------------------------------------------------------------------


def _update_cache(session: Session, entry: MarketAsset, asset_type: AssetType) -> dict | None:
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
    symbol_hint: str | None = None,
) -> MarketAsset | None:
    """
    Public entry-point: find or auto-create a MarketAsset for a given key.

    lookup_key  — ISIN for stocks, ticker symbol for crypto/fiat.
    symbol_hint — known ticker to try first for get_info (avoids a search round-trip).
                  Useful when the caller already has the symbol (e.g. from a form field).
    """
    # Fast path: already in DB
    existing = session.exec(
        select(MarketAsset).where(MarketAsset.asset_key == lookup_key)
    ).first()
    if existing:
        if _ensure_asset_type(existing, asset_type):
            session.add(existing)
            session.commit()
            session.refresh(existing)
        return existing

    return _create_market_asset_entry(session, lookup_key, asset_type, symbol_hint=symbol_hint)


def _create_market_asset_entry(
    session: Session, lookup_key: str, asset_type: AssetType, symbol_hint: str | None = None
) -> MarketAsset | None:
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
        select(MarketAsset).where(MarketAsset.asset_key == lookup_key)
    ).first()
    if existing:
        if _ensure_asset_type(existing, asset_type):
            session.add(existing)
            session.commit()
            session.refresh(existing)
        return existing

    ma = MarketAsset(
        asset_key=lookup_key,
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
            select(MarketAsset).where(MarketAsset.asset_key == lookup_key)
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


def get_all_assets(
    user_uuid: str,
    master_key: str,
    session: Session,
    only_owned: bool,
    asset_type: AssetType | None,
    limit: int | None,
    
) -> list[dict]:
    """Get all market assets, excluding FIAT, with optional filtering by asset type and owned status."""
    statement = select(MarketAsset).where(MarketAsset.asset_type != AssetType.FIAT)
    if asset_type is not None:
        statement = statement.where(MarketAsset.asset_type == asset_type)
    
    assets = session.exec(statement).all()
    
    owned_keys = set()
    user_bidx = hash_index(user_uuid, master_key)

    if asset_type is None or asset_type == AssetType.STOCK:
        # Stocks
        stock_accounts = session.exec(
            select(StockAccount).where(StockAccount.user_uuid_bidx == user_bidx)
        ).all()
        stock_agg = {}
        for account in stock_accounts:
            account_bidx = hash_index(account.uuid, master_key)
            txs = session.exec(
                select(StockTransaction).where(StockTransaction.account_id_bidx == account_bidx)
            ).all()
            for tx in txs:
                try:
                    key = decrypt_data(tx.asset_key_enc, master_key).upper()
                    tx_type = decrypt_data(tx.type_enc, master_key)
                    amount = Decimal(decrypt_data(tx.amount_enc, master_key))
                    if key not in stock_agg:
                        stock_agg[key] = Decimal("0")
                    if tx_type in ("BUY", "DIVIDEND", "DEPOSIT"):
                        stock_agg[key] += amount
                    elif tx_type == "SELL":
                        stock_agg[key] -= amount
                except Exception:
                    continue
        for key, amt in stock_agg.items():
            if amt > 0:
                owned_keys.add(key)

    if asset_type is None or asset_type == AssetType.CRYPTO:
        from dtos.crypto import FIAT_ASSET_KEYS
        # Crypto
        crypto_accounts = session.exec(
            select(CryptoAccount).where(CryptoAccount.user_uuid_bidx == user_bidx)
        ).all()
        crypto_agg = {}
        for account in crypto_accounts:
            account_bidx = hash_index(account.uuid, master_key)
            txs = session.exec(
                select(CryptoTransaction).where(CryptoTransaction.account_id_bidx == account_bidx)
            ).all()
            for tx in txs:
                try:
                    key = decrypt_data(tx.asset_key_enc, master_key).upper()
                    tx_type = decrypt_data(tx.type_enc, master_key)
                    amount = Decimal(decrypt_data(tx.amount_enc, master_key))
                    if key in FIAT_ASSET_KEYS or tx_type == "ANCHOR":
                        continue
                    if key not in crypto_agg:
                        crypto_agg[key] = Decimal("0")
                    if tx_type in ("BUY", "REWARD", "DEPOSIT"):
                        crypto_agg[key] += amount
                    elif tx_type in ("SPEND", "TRANSFER", "WITHDRAW", "FEE"):
                        crypto_agg[key] -= amount
                except Exception:
                    continue
        for key, amt in crypto_agg.items():
            if amt > 0:
                owned_keys.add(key)

    # Filter by owned if requested
    if only_owned:
        assets = [a for a in assets if a.asset_key and a.asset_key.upper() in owned_keys]
    
    # Sort: owned assets first, then keep original database ordering
    sorted_assets = sorted(assets, key=lambda a: 0 if (a.asset_key and a.asset_key.upper() in owned_keys) else 1)
    
    # Apply limit
    if limit is not None and limit > 0:
        sorted_assets = sorted_assets[:limit]
        
    return [a.model_dump() for a in sorted_assets]


def get_stock_price(
    session: Session,
    asset_key: str,
    db_only: bool = False,
    as_of: date | None = None,
) -> Decimal | None:
    """Get current market price for a Stock (lookup by ISIN)."""
    _, price = _get_market_info_internal(
        session,
        asset_key,
        AssetType.STOCK,
        db_only=db_only,
        as_of=as_of,
    )
    return price


def get_crypto_price(
    session: Session,
    symbol: str,
    db_only: bool = False,
    as_of: date | None = None,
) -> Decimal | None:
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
    as_of: date | None = None,
) -> tuple[str | None, Decimal | None]:
    """Shared logic for fetching info. Auto-creates missing entries."""
    target_date = as_of or date.today()
    today = date.today()

    cached = session.exec(
        select(MarketAsset).where(MarketAsset.asset_key == lookup_key)
    ).first()

    if not cached:
        if db_only:
            # Asset unknown → nothing in DB, return empty immediately (no API call)
            return None, None
        cached = _create_market_asset_entry(session, lookup_key, asset_type)
        if not cached:
            return None, None

    if db_only:
        # Return latest cached price up to target_date (no API call).
        latest = _get_latest_price_entry_as_of(session, cached.id, target_date)
        return cached.name, (latest.price if latest else None)

    # Historical valuation mode for past dates.
    if target_date < today:
        latest = _get_latest_price_entry_as_of(session, cached.id, target_date)
        # If we have an exact match for the requested past date, return it
        if latest and latest.price_date == target_date:
            return cached.name, latest.price

        try:
            backfill_price_history(session, lookup_key, asset_type, target_date)
        except Exception:
            logger.debug(
                "get_%s_price: historical backfill failed for %s on %s",
                asset_type.value.lower(),
                lookup_key,
                target_date,
                exc_info=True,
            )

        latest = _get_latest_price_entry_as_of(session, cached.id, target_date)
        if latest and latest.price_date == target_date:
            return cached.name, latest.price

        return cached.name, None

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
    asset_key: str,
    db_only: bool = False,
    as_of: date | None = None,
) -> tuple[str | None, Decimal | None]:
    """Get (Name, Price) for a Stock."""
    return _get_market_info_internal(
        session,
        asset_key,
        AssetType.STOCK,
        db_only=db_only,
        as_of=as_of,
    )


def get_crypto_info(
    session: Session,
    symbol: str,
    db_only: bool = False,
    as_of: date | None = None,
) -> tuple[str | None, Decimal | None]:
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


def update_all_prices_daily() -> dict:
    """
    Single entry-point for the nightly CRON job.

    * Stocks  — Yahoo Finance, batches of 50, 2 s sleep
    * Cryptos — CoinMarketCap, batches of 100, 3 s sleep
    * Bulk upsert into market_price_history (one price per asset per day)

    Returns the counters the scheduler records in `job_runs`.
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

        # Prices have just moved, so this is the one moment where a pick can
        # newly have reached its target. Isolated: a failure here must not make
        # the price update look like it failed.
        notified = 0
        try:
            from services.notification import check_pick_targets
            notified = check_pick_targets(session)
            if notified:
                logger.info("CRON update_all_prices_daily: %d pick target notifications", notified)
        except Exception as exc:
            logger.error("Pick target check failed: %s", exc)

        return {"prices": len(prices_collected), "pick_notifications": notified}


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


def _last_rate_before(session: Session, asset_id: int, day: date) -> Decimal | None:
    """The most recent stored price strictly before `day`, or None.

    Seeds the carry-forward when a range opens on a day the market was closed —
    without it, a series starting on a Saturday would have nothing behind it.
    """
    row = session.exec(
        select(MarketPriceHistory)
        .where(
            MarketPriceHistory.market_asset_id == asset_id,
            MarketPriceHistory.price_date < day,
        )
        .order_by(MarketPriceHistory.price_date.desc())
    ).first()
    return row.price if row else None


def _date_range(from_date: date, to_date: date):
    current = from_date
    while current <= to_date:
        yield current
        current += timedelta(days=1)


def _get_or_create_forex_asset(session: Session, currency: str) -> MarketAsset:
    """Return (or auto-create) a FIAT MarketAsset tracking currency vs EUR."""
    asset = session.exec(select(MarketAsset).where(MarketAsset.asset_key == currency)).first()
    if not asset:
        asset = MarketAsset(
            asset_key=currency,
            symbol=currency,
            name=currency,
            asset_type=AssetType.FIAT,
        )
        session.add(asset)
        try:
            session.commit()
        except Exception:
            session.rollback()
            asset = session.exec(select(MarketAsset).where(MarketAsset.asset_key == currency)).first()
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

    # Dates the market never published a rate for — weekends, public holidays —
    # carry the last rate published before them, the way a closed market is
    # priced everywhere else (the ECB itself publishes on business days only, and
    # a Saturday reads at Friday's rate). Filling them with *today's* rate
    # instead stamped a 2022 Saturday with a 2026 one, and roughly three days in
    # ten of a multi-year series are such days.
    carried = _last_rate_before(session, asset.id, from_date)
    spot: Decimal | None = None
    for d in _date_range(from_date, to_date):
        if d in result:
            carried = result[d]
            continue
        if carried is not None:
            result[d] = carried
            continue
        # Nothing was ever published before this date, so there is nothing to
        # carry: the spot rate is the only value available. Fetched once, and
        # only when that case actually arises.
        if spot is None:
            spot = get_exchange_rate(session, currency, "EUR")
        result[d] = spot

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

    # Skip API call if all calendar days up to yesterday are already in DB
    yesterday = to_date - timedelta(days=1)
    if yesterday >= from_date:
        expected_dates = set(_date_range(from_date, yesterday))
        if expected_dates.issubset(existing_dates):
            return 0, len(existing_dates)

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

    # Skip API call if all calendar days up to yesterday are already in DB
    yesterday = to_date - timedelta(days=1)
    if yesterday >= from_date:
        expected_dates = set(_date_range(from_date, yesterday))
        if expected_dates.issubset(existing_dates):
            return 0, len(existing_dates)

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
        select(MarketAsset).where(MarketAsset.asset_key == lookup_key)
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


# ---------------------------------------------------------------------------
# Per-asset price timeline — the asset's curve with the user's own trades on it
# ---------------------------------------------------------------------------

_ZERO_EUR = Decimal("0")


def _price_lookup(points: list[dict]) -> Callable[[date], Decimal | None]:
    """Build a date → price reader that falls back to the last known close.

    Weekends, holidays and gaps left by a failed backfill all mean a trade can
    land on a day with no row; the price that mattered then is the last one
    quoted before it.
    """
    dates = [p["date"] for p in points]
    prices = [p["price"] for p in points]

    def read(day: date) -> Decimal | None:
        if not dates:
            return None
        index = bisect.bisect_right(dates, day)
        if index == 0:
            # Trade predates every price we hold — the first close is the
            # closest honest answer, and leaving it None would drop the marker.
            return prices[0]
        return prices[index - 1]

    return read


def _collect_account_transactions(
    session: Session,
    user_uuid: str,
    master_key: str,
    account_id: str | None,
) -> tuple[list[list[TransactionResponse]], list[list[TransactionResponse]]]:
    """(stock accounts, crypto accounts), each one its own list of rows, oldest first.

    Kept per account rather than flattened, because cost basis is an account-level
    quantity: pooling two wallets would let a sale in one draw cost out of the
    other. Kept **unfiltered by asset** too — a crypto BUY row says how much of
    the asset arrived but not what it cost, because the euros left on another row
    of the same group (an ANCHOR, or a fiat SPEND), and those rows are booked
    under EUR. Filtering by asset first would throw away the only rows that price
    the trade.

    Imported locally: both transaction services read prices from this module, so
    importing them at module level would close the cycle.
    """
    from services.crypto_account import get_user_crypto_accounts
    from services.crypto_transaction import get_account_transactions as get_crypto_txs
    from services.stock_account import get_user_stock_accounts
    from services.stock_transaction import get_account_transactions as get_stock_txs

    def gather(list_accounts, list_transactions) -> list[list[TransactionResponse]]:
        per_account: list[list[TransactionResponse]] = []
        for account in list_accounts(session, user_uuid, master_key):
            # account_id is the caller's claim; only the accounts this user owns
            # were listed above, so filtering here is also the ownership check.
            if account_id and account.id != account_id:
                continue
            rows = list_transactions(session, account.id, master_key)
            if rows:
                per_account.append(sorted(rows, key=lambda tx: tx.executed_at))
        return per_account

    return (
        gather(get_user_stock_accounts, get_stock_txs),
        gather(get_user_crypto_accounts, get_crypto_txs),
    )


def _merge_timelines(
    per_account: list[tuple[list[dict], Decimal, Decimal]],
) -> tuple[list[dict], Decimal, Decimal]:
    """Interleave several accounts' markers and carry a portfolio-wide unit cost.

    Each account keeps its own basis — a sale in one wallet must not draw cost
    out of another — so the engines run per account and only the totals meet
    here. Between two markers of the same account, a row that moves the position
    without a marker of its own (a fee paid in the asset, a transfer out) is not
    reflected in the line until that account's next marker; the quantity and unit
    cost returned below come from each engine's final state and stay exact.
    """
    ordered = sorted(
        (
            (event["date"], index, event)
            for index, (events, _, _) in enumerate(per_account)
            for event in events
        ),
        # The account index only breaks ties, so each account's own rows keep
        # the order its engine produced them in.
        key=lambda item: (item[0], item[1]),
    )

    state: dict[int, tuple[Decimal, Decimal]] = {}
    merged: list[dict] = []
    for _, index, event in ordered:
        state[index] = (event.pop("_quantity"), event.pop("_cost"))
        held = sum((held for held, _ in state.values()), _ZERO_EUR)
        spent = sum((spent for _, spent in state.values()), _ZERO_EUR)
        event["cost_basis_after"] = round(spent / held, 8) if held > 0 else None
        merged.append(event)

    return (
        merged,
        sum((quantity for _, quantity, _ in per_account), _ZERO_EUR),
        sum((cost for _, _, cost in per_account), _ZERO_EUR),
    )


def _crypto_group_flows(
    transactions: list[TransactionResponse],
) -> tuple[dict[str, Decimal], dict[str, Decimal], dict[str, Decimal]]:
    """Euro flows of each atomic group: (anchors, fiat spent, fiat received).

    Mirrors get_crypto_account_summary, which is the app's reference for what a
    crypto trade cost.
    """
    from dtos.crypto import FIAT_ASSET_KEYS

    anchors: dict[str, Decimal] = {}
    fiat_spent: dict[str, Decimal] = {}
    fiat_received: dict[str, Decimal] = {}

    for tx in transactions:
        if not tx.group_uuid:
            continue
        value = Decimal(tx.amount or 0) * Decimal(tx.price_per_unit or 0)
        is_fiat = (tx.asset_key or "").upper() in FIAT_ASSET_KEYS

        if tx.type == "ANCHOR":
            anchors[tx.group_uuid] = anchors.get(tx.group_uuid, _ZERO_EUR) + value
        elif tx.type == "SPEND" and is_fiat:
            fiat_spent[tx.group_uuid] = fiat_spent.get(tx.group_uuid, _ZERO_EUR) + value
        elif tx.type == "DEPOSIT" and is_fiat:
            # Fiat received inside a group: the proceeds side of a sell-to-fiat.
            fiat_received[tx.group_uuid] = fiat_received.get(tx.group_uuid, _ZERO_EUR) + value

    return anchors, fiat_spent, fiat_received



def _crypto_timeline_events(
    transactions: list[TransactionResponse],
    asset_key: str,
    price_at: Callable[[date], Decimal | None],
) -> tuple[list[dict], Decimal, Decimal]:
    """Markers and running cost basis for one crypto asset.

    Follows get_crypto_account_summary exactly, including the parts that carry no
    marker: a FEE paid in the asset takes quantity away without touching the
    basis, and a TRANSFER out removes both — skip them and the unit cost drifts.
    Fees stay out of the basis here, the opposite of the stock ledger, because
    that is how the crypto summary computes the PRU shown everywhere else.
    """
    anchors, fiat_spent, fiat_received = _crypto_group_flows(transactions)

    buy_cost: dict[str, Decimal] = {}
    for tx in transactions:
        if tx.type == "BUY" and tx.group_uuid:
            if tx.group_uuid in anchors:
                buy_cost[tx.id] = anchors[tx.group_uuid]
            elif tx.group_uuid in fiat_spent:
                buy_cost[tx.id] = fiat_spent[tx.group_uuid]
            else:
                buy_cost[tx.id] = _ZERO_EUR

    def proceeds_of(group: str) -> Decimal | None:
        """Euros a disposal brought in: fiat received, else the trade's anchor."""
        if group in fiat_received:
            return fiat_received[group]
        if group in anchors:
            return anchors[group]
        return None

    key = asset_key.upper()
    quantity = _ZERO_EUR
    cost = _ZERO_EUR
    events: list[dict] = []

    for tx in transactions:
        if (tx.asset_key or "").upper() != key:
            continue

        tx_type = (tx.type or "").upper()
        day = tx.executed_at.date()
        amount = Decimal(tx.amount or 0)
        marker: tuple[str, Decimal | None, Decimal] | None = None

        if tx_type == "BUY":
            spent = buy_cost.get(tx.id, amount * Decimal(tx.price_per_unit or 0))
            previous = quantity
            quantity += amount
            if previous < 0 and amount > 0:
                # Buying back into a negative balance: only the part that
                # survives the repayment carries cost.
                surviving = max(quantity, _ZERO_EUR)
                cost += spent * (surviving / amount)
            else:
                cost += spent
            if amount > 0 and spent > 0:
                marker = ("BUY", spent / amount, -spent)

        elif tx_type in ("SPEND", "TRANSFER"):
            if quantity > 0:
                fraction = min(amount / quantity, Decimal("1"))
                cost = max(cost - cost * fraction, _ZERO_EUR)
            quantity -= amount
            # Only a SPEND inside a group is a disposal with euros behind it; a
            # TRANSFER moves the asset to another wallet at no price at all.
            if tx_type == "SPEND" and tx.group_uuid and amount > 0:
                proceeds = proceeds_of(tx.group_uuid)
                if proceeds is not None and proceeds > 0:
                    marker = ("SELL", proceeds / amount, proceeds)

        elif tx_type == "WITHDRAW":
            if quantity > 0:
                fraction = min(amount / quantity, Decimal("1"))
                cost = max(cost - cost * fraction, _ZERO_EUR)
            quantity -= amount

        elif tx_type in ("REWARD", "DEPOSIT"):
            # Arrives as units of the asset at no cost, which is what drags the
            # unit cost down. No price of its own, so the marker rides the curve.
            quantity += amount
            plot_price = price_at(day)
            marker = ("INCOME", plot_price, amount * (plot_price or _ZERO_EUR))

        elif tx_type == "FEE":
            # Paid in the asset itself: takes quantity without touching the
            # basis, so the unit cost rises. Not a decision, so no marker.
            quantity -= amount

        else:  # ANCHOR and anything unknown
            continue

        if marker is not None:
            kind, plot_price, total = marker
            events.append(
                _timeline_event(day, kind, amount, plot_price, total, quantity, cost)
            )

    return events, quantity, cost


def _stock_timeline_events(
    transactions: list[TransactionResponse],
    asset_key: str,
    rate_for: Callable[[str, date], Decimal],
    price_at: Callable[[date], Decimal | None],
) -> tuple[list[dict], Decimal, Decimal]:
    """Markers and running cost basis for one stock line.

    Follows get_stock_account_summary: the price is carried by the row itself,
    fees are part of the basis, and a sale removes cost in proportion to the
    quantity it takes.
    """
    key = asset_key.upper()
    quantity = _ZERO_EUR
    cost = _ZERO_EUR
    events: list[dict] = []

    for tx in transactions:
        if (tx.asset_key or "").upper() != key:
            continue

        tx_type = (tx.type or "").upper()
        day = tx.executed_at.date()
        rate = rate_for(tx.currency, day)
        unit_price = Decimal(tx.price_per_unit or 0) * rate
        fees = Decimal(tx.fees or 0) * rate
        amount = Decimal(tx.amount or 0)

        if tx_type == "BUY":
            spent = amount * unit_price + fees
            quantity += amount
            cost += spent
            kind, plot_price, total = "BUY", unit_price, -spent

        elif tx_type == "SELL":
            proceeds = amount * unit_price - fees
            if quantity > 0:
                fraction = min(amount / quantity, Decimal("1"))
                cost = max(cost - cost * fraction, _ZERO_EUR)
                quantity = max(quantity - amount, _ZERO_EUR)
            kind, plot_price, total = "SELL", unit_price, proceeds

        elif tx_type == "DIVIDEND":
            # Cash income: it has no price of its own, so the marker rides the
            # curve, and it leaves the position untouched.
            kind = "INCOME"
            plot_price = price_at(day)
            total = amount * unit_price - fees

        else:  # EUR deposits and withdrawals never reach a listed asset
            continue

        events.append(
            _timeline_event(day, kind, amount, plot_price, total, quantity, cost)
        )

    return events, quantity, cost


def _timeline_event(
    day: date,
    kind: str,
    amount: Decimal,
    plot_price: Decimal | None,
    total: Decimal,
    quantity: Decimal,
    cost: Decimal,
) -> dict:
    """One marker, carrying the account's position right after the trade.

    The underscored keys are the account's running state, which _merge_timelines
    consumes to work out the unit cost to display and then strips.
    """
    return {
        "date": day,
        "type": kind,
        "quantity": amount,
        "price": round(plot_price, 8) if plot_price is not None else None,
        "total": round(total, 2),
        "_quantity": quantity,
        "_cost": cost,
    }


def get_asset_price_timeline(
    session: Session,
    user_uuid: str,
    master_key: str,
    asset_key: str,
    account_id: str | None = None,
) -> dict:
    """Price history of *asset_key* since the user first traded it, with their trades.

    Everything comes back in EUR. ``market_price_history`` is already stored
    converted, and every ledger today records ``currency="EUR"`` too, so the
    conversion below is a no-op in practice — it is there so that the day a
    price is stored in the currency it was executed in, a 150 USD buy does not
    land on a 138 EUR curve.
    """
    from dtos.crypto import FIAT_ASSET_KEYS

    key = (asset_key or "").upper()
    if key in FIAT_ASSET_KEYS:
        # Cash is not a position with a curve; it is what the curve is priced in.
        raise ValueError(f"Pas de cours pour une devise : {asset_key!r}")

    asset = session.exec(select(MarketAsset).where(MarketAsset.asset_key == key)).first()
    if not asset:
        raise ValueError(f"Actif introuvable : {asset_key!r}")

    stock_accounts, crypto_accounts = _collect_account_transactions(
        session, user_uuid, master_key, account_id
    )

    def holds(accounts: list[list[TransactionResponse]]) -> bool:
        return any(
            (tx.asset_key or "").upper() == key for rows in accounts for tx in rows
        )

    asset_type = asset.asset_type
    if asset_type == AssetType.CRYPTO:
        is_crypto = True
    elif asset_type == AssetType.STOCK:
        is_crypto = False
    else:
        # An asset whose type was never resolved: let the ledger holding it decide.
        is_crypto = holds(crypto_accounts) and not holds(stock_accounts)

    ledger_accounts = crypto_accounts if is_crypto else stock_accounts
    asset_rows = sorted(
        (tx for rows in ledger_accounts for tx in rows if (tx.asset_key or "").upper() == key),
        key=lambda tx: tx.executed_at,
    )

    today = date.today()
    floor_date = today - timedelta(days=_MAX_BACKFILL_DAYS)
    if asset_rows:
        from_date = max(asset_rows[0].executed_at.date(), floor_date)
    else:
        # No trade to anchor on (a position read from an import that carries no
        # ledger, say): a year of context still beats an empty chart.
        from_date = max(today - timedelta(days=365), floor_date)

    if asset_type in (AssetType.STOCK, AssetType.CRYPTO):
        ensure_price_history(session, key, asset_type, from_date)

    rows = session.exec(
        select(MarketPriceHistory)
        .where(
            MarketPriceHistory.market_asset_id == asset.id,
            MarketPriceHistory.price_date >= from_date,
            MarketPriceHistory.price_date <= today,
        )
        .order_by(MarketPriceHistory.price_date)
    ).all()
    points = [{"date": row.price_date, "price": row.price} for row in rows]
    price_at = _price_lookup(points)

    if is_crypto:
        per_account = [
            _crypto_timeline_events(rows, key, price_at) for rows in ledger_accounts
        ]
    else:
        # One rate table per foreign currency, fetched once for the whole window
        # rather than per transaction.
        rates_by_currency: dict[str, dict[date, Decimal]] = {}
        for tx in asset_rows:
            currency = (tx.currency or "EUR").upper()
            if currency == "EUR" or currency in rates_by_currency:
                continue
            rates_by_currency[currency] = get_historical_exchange_rates_db(
                session, currency, from_date, today
            )

        def rate_for(currency: str, day: date) -> Decimal:
            currency = (currency or "EUR").upper()
            if currency == "EUR":
                return Decimal("1")
            table = rates_by_currency.get(currency, {})
            if day in table:
                return table[day]
            # Same reasoning as the price fallback: a missing day means no quote
            # that day, not a rate of zero.
            earlier = [d for d in table if d <= day]
            if earlier:
                return table[max(earlier)]
            return get_exchange_rate(session, currency, "EUR")

        per_account = [
            _stock_timeline_events(rows, key, rate_for, price_at)
            for rows in ledger_accounts
        ]

    events, quantity, cost = _merge_timelines(per_account)

    return {
        "asset_key": key,
        "symbol": asset.symbol,
        "name": asset.name,
        "asset_type": asset_type,
        "currency": "EUR",
        "points": points,
        "events": events,
        "average_buy_price": round(cost / quantity, 8) if quantity > 0 else None,
        "quantity_held": quantity,
        "current_price": get_latest_price(session, asset.id),
    }
