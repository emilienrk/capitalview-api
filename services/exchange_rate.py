"""Exchange rate service with in-memory caching.

Used primarily to convert crypto values (USD) to the portfolio base currency (EUR)
in the dashboard aggregation.
"""

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

import yfinance as yf

from dtos import AccountSummaryResponse, PositionResponse

logger = logging.getLogger(__name__)

# ── In-memory cache ────────────────────────────────────────────
_cache: dict[str, dict] = {}
CACHE_TTL = timedelta(hours=1)

# Fallback rate if external fetch fails (also mirrored in frontend useCurrencyToggle.ts)
_FALLBACK_USD_EUR = Decimal("0.92")


def _cache_key(from_currency: str, to_currency: str) -> str:
    return f"{from_currency}_{to_currency}"


def get_exchange_rate(
    from_currency: str = "USD",
    to_currency: str = "EUR",
) -> Decimal:
    """Return the exchange rate *from_currency* → *to_currency*.

    Results are cached in memory for ``CACHE_TTL``.  If the external
    fetch fails, a hardcoded fallback rate is returned so that the
    dashboard still works (with slightly stale data).
    """
    if from_currency == to_currency:
        return Decimal("1")

    key = _cache_key(from_currency, to_currency)
    now = datetime.now(timezone.utc)

    # Check cache
    if key in _cache:
        entry = _cache[key]
        if entry["expires"] > now:
            return entry["rate"]

    rate = _fetch_rate_yahoo(from_currency, to_currency)

    if rate is None:
        # Try inverse
        inverse = _fetch_rate_yahoo(to_currency, from_currency)
        if inverse and inverse > 0:
            rate = Decimal("1") / inverse

    if rate is None:
        # Use fallback
        logger.warning(
            "Could not fetch %s→%s rate, using fallback", from_currency, to_currency
        )
        if from_currency == "USD" and to_currency == "EUR":
            rate = _FALLBACK_USD_EUR
        elif from_currency == "EUR" and to_currency == "USD":
            rate = Decimal("1") / _FALLBACK_USD_EUR
        else:
            rate = Decimal("1")

    _cache[key] = {"rate": rate, "expires": now + CACHE_TTL}
    return rate


def _fetch_rate_yahoo(from_currency: str, to_currency: str) -> Optional[Decimal]:
    """Fetch live exchange rate via yfinance (e.g. USDEUR=X)."""
    symbol = f"{from_currency}{to_currency}=X"
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.info or {}
        price = (
            info.get("regularMarketPrice")
            or info.get("ask")
            or info.get("bid")
        )
        if price and price > 0:
            return Decimal(str(price))

        # Fallback to fast_info
        if hasattr(ticker, "fast_info"):
            try:
                price = ticker.fast_info.last_price
                if price and price > 0:
                    return Decimal(str(price))
            except (AttributeError, TypeError):
                pass
    except Exception:
        logger.warning("yfinance fetch failed for %s", symbol, exc_info=True)
    return None


def convert_amount(
    amount: Decimal,
    from_currency: str = "USD",
    to_currency: str = "EUR",
) -> Decimal:
    """Convert *amount* from one currency to another."""
    if from_currency == to_currency:
        return amount
    rate = get_exchange_rate(from_currency, to_currency)
    return amount * rate


# ── DTO conversion helpers ─────────────────────────────────────

def convert_position_to_eur(pos: PositionResponse, rate: Decimal) -> PositionResponse:
    """Convert a single USD position to EUR using model_copy to preserve any new fields."""
    return pos.model_copy(update={
        "average_buy_price": round(pos.average_buy_price * rate, 4),
        "total_invested": round(pos.total_invested * rate, 2),
        "total_fees": round(pos.total_fees * rate, 2),
        "currency": "EUR",
        "current_price": round(pos.current_price * rate, 4) if pos.current_price else None,
        "current_value": round(pos.current_value * rate, 2) if pos.current_value else None,
        "profit_loss": round(pos.profit_loss * rate, 2) if pos.profit_loss else None,
    })


def convert_account_to_eur(
    account: AccountSummaryResponse, rate: Decimal
) -> AccountSummaryResponse:
    """Convert a USD account summary to EUR using model_copy to preserve any new fields."""
    positions_eur = [convert_position_to_eur(p, rate) for p in account.positions]
    return account.model_copy(update={
        "total_invested": round(account.total_invested * rate, 2),
        "total_fees": round(account.total_fees * rate, 2),
        "currency": "EUR",
        "current_value": round(account.current_value * rate, 2) if account.current_value else None,
        "profit_loss": round(account.profit_loss * rate, 2) if account.profit_loss else None,
        "positions": positions_eur,
    })
