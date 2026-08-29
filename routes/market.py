"""Market data routes — price backfill and related utilities."""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import Session

from database import get_session
from models import User
from models.currency import BASE_CURRENCY, SUPPORTED_CURRENCIES
from models.enums import AssetType
from services.auth import get_current_user, get_master_key
from services.market import backfill_price_history, get_all_assets, get_non_trading_days

router = APIRouter(prefix="/market", tags=["Market"])


class PriceBackfillRequest(BaseModel):
    """Request body for the price backfill endpoint."""

    lookup_key: str
    """ISIN for stocks, symbol (e.g. 'BTC') for crypto."""
    asset_type: AssetType
    from_date: date
    """First date to backfill. Cannot be in the future or more than 10 years ago."""


class PriceBackfillResponse(BaseModel):
    """Result of a price backfill operation."""

    lookup_key: str
    symbol: str | None
    name: str | None
    asset_type: AssetType
    from_date: date
    to_date: date
    inserted: int
    """Number of new price rows inserted."""
    skipped: int
    """Number of dates that already had a price (not overwritten)."""


class CurrencyOption(BaseModel):
    """One currency the interface may offer."""

    code: str
    """ISO 4217 alphabetic code."""
    name: str


class SupportedCurrencies(BaseModel):
    """The currencies the interface offers, and the one everything is totalled in."""

    base: str
    currencies: list[CurrencyOption]


@router.get("/currencies", response_model=SupportedCurrencies)
def get_supported_currencies():
    """The curated list the web app builds its currency pickers from.

    Unauthenticated on purpose: it is a static catalogue, identical for
    everyone, and carries nothing about the caller. Serving it here rather than
    duplicating it in the web app is the whole point — the list moved three
    times out of sync before it lived in one place.

    Not a validation whitelist: what the API enforces on an account is that a
    currency can actually be converted, which is a market-data question, not a
    membership one.
    """
    return SupportedCurrencies(
        base=BASE_CURRENCY,
        currencies=[CurrencyOption(code=c.code, name=c.name) for c in SUPPORTED_CURRENCIES],
    )


@router.post(
    "/backfill",
    response_model=PriceBackfillResponse,
    summary="Backfill prix historiques",
    description=(
        "Récupère et stocke les prix journaliers manquants pour un actif "
        "depuis `from_date` jusqu'à aujourd'hui. "
        "Utile lorsqu'une transaction passée est saisie sans historique de prix en base. "
        "Sources : Yahoo Finance (actions) · CoinGecko (crypto)."
    ),
)
def backfill_prices(
    data: PriceBackfillRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Session = Depends(get_session),
) -> PriceBackfillResponse:
    """
    Backfill historical prices for an asset from a given date to today.

    - **Stocks** : uses Yahoo Finance (yfinance), all dates fetched in one call.
    - **Crypto** : uses CoinGecko public API, one call per symbol.
    Existing rows are preserved (no overwrite). Rate-limiting sleeps are
    embedded to avoid being banned by external APIs.
    """
    try:
        result = backfill_price_history(session, data.lookup_key, data.asset_type, data.from_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return PriceBackfillResponse(
        lookup_key=data.lookup_key,
        symbol=result["symbol"],
        name=result["name"],
        asset_type=data.asset_type,
        from_date=result["from_date"],
        to_date=result["to_date"],
        inserted=result["inserted"],
        skipped=result["skipped"],
    )


@router.get(
    "/assets",
    response_model=list[dict],
    summary="Obtenir tous les actifs",
    description=(
        "Récupère tous les actifs enregistrés en base, en excluant les devises FIAT. "
        "Permet de filtrer par type d'actif et par possession (owned). "
        "Si owned=False, les actifs possédés par l'utilisateur sont tout de même triés en premier."
    ),
)
def get_market_assets(
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    asset_type: AssetType | None = None,
    limit: int | None = None,
    session: Session = Depends(get_session),
) -> list[dict]:
    return get_all_assets(
        user_uuid=current_user.uuid,
        master_key=master_key,
        session=session,
        only_owned=True,
        asset_type=asset_type,
        limit=limit,
    )


class NonTradingDaysResponse(BaseModel):
    """Dates closed on every requested exchange over the range."""

    days: list[date]


@router.get(
    "/non-trading-days",
    response_model=NonTradingDaysResponse,
    summary="Jours non cotés (weekends + fériés)",
    description=(
        "Renvoie les dates de `[from_date, to_date]` fermées sur **toutes** les "
        "places boursières demandées (union des sessions). Permet de retirer les "
        "segments plats (weekends, jours fériés) des graphes actions. "
        "Sans MIC connu/supporté, renvoie une liste vide (aucun filtrage)."
    ),
)
def non_trading_days(
    current_user: Annotated[User, Depends(get_current_user)],
    from_date: date,
    to_date: date,
    mic: Annotated[list[str], Query()] = [],
) -> NonTradingDaysResponse:
    return NonTradingDaysResponse(days=get_non_trading_days(mic, from_date, to_date))

