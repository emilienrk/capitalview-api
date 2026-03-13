"""Market data routes — price backfill and related utilities."""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from database import get_session
from models import User
from models.enums import AssetType
from services.auth import get_current_user
from services.market import backfill_price_history

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
