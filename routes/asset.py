"""Personal assets CRUD routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from database import get_session
from models import User
from services.auth import get_current_user, get_master_key
from dtos.asset import (
    AssetCreate,
    AssetUpdate,
    AssetSell,
    AssetResponse,
    AssetSummaryResponse,
    AssetValuationCreate,
    AssetValuationResponse,
)
from services.asset import (
    create_asset,
    get_asset,
    get_user_assets,
    update_asset,
    delete_asset as service_delete_asset,
    sell_asset as service_sell_asset,
    create_valuation,
    get_asset_valuations,
    delete_valuation as service_delete_valuation,
)

router = APIRouter(prefix="/assets", tags=["Assets"])


# ============== ASSETS CRUD ==============

@router.post("", response_model=AssetResponse, status_code=201)
def create(
    data: AssetCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """Create a new personal asset."""
    return create_asset(session, data, current_user.uuid, master_key)


@router.get("", response_model=AssetSummaryResponse)
def list_assets(
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """Get all personal assets with summary for current user."""
    return get_user_assets(session, current_user.uuid, master_key)


@router.get("/{asset_id}", response_model=AssetResponse)
def get_one(
    asset_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """Get a specific personal asset."""
    asset = get_asset(session, asset_id, current_user.uuid, master_key)
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    return asset


@router.put("/{asset_id}", response_model=AssetResponse)
def update(
    asset_id: str,
    data: AssetUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """Update a personal asset."""
    from models.asset import Asset as AssetModel
    existing = get_asset(session, asset_id, current_user.uuid, master_key)
    if not existing:
        raise HTTPException(status_code=404, detail="Asset not found")

    asset_model = session.get(AssetModel, asset_id)
    return update_asset(session, asset_model, data, master_key)


@router.delete("/{asset_id}", status_code=204)
def delete(
    asset_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """Delete a personal asset and its valuation history."""
    existing = get_asset(session, asset_id, current_user.uuid, master_key)
    if not existing:
        raise HTTPException(status_code=404, detail="Asset not found")

    service_delete_asset(session, asset_id)
    return None


@router.post("/{asset_id}/sell", response_model=AssetResponse)
def sell(
    asset_id: str,
    data: AssetSell,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """Mark a personal asset as sold."""
    existing = get_asset(session, asset_id, current_user.uuid, master_key)
    if not existing:
        raise HTTPException(status_code=404, detail="Asset not found")

    return service_sell_asset(session, asset_id, data, master_key)


# ============== VALUATION HISTORY ==============

@router.get("/{asset_id}/valuations", response_model=list[AssetValuationResponse])
def list_valuations(
    asset_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """Get valuation history for an asset."""
    existing = get_asset(session, asset_id, current_user.uuid, master_key)
    if not existing:
        raise HTTPException(status_code=404, detail="Asset not found")

    return get_asset_valuations(session, asset_id, master_key)


@router.post("/{asset_id}/valuations", response_model=AssetValuationResponse, status_code=201)
def add_valuation(
    asset_id: str,
    data: AssetValuationCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """Add a new valuation entry for an asset."""
    existing = get_asset(session, asset_id, current_user.uuid, master_key)
    if not existing:
        raise HTTPException(status_code=404, detail="Asset not found")

    return create_valuation(session, asset_id, data, master_key)


@router.delete("/{asset_id}/valuations/{valuation_id}", status_code=204)
def remove_valuation(
    asset_id: str,
    valuation_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """Delete a valuation entry."""
    existing = get_asset(session, asset_id, current_user.uuid, master_key)
    if not existing:
        raise HTTPException(status_code=404, detail="Asset not found")

    if not service_delete_valuation(session, valuation_id):
        raise HTTPException(status_code=404, detail="Valuation not found")

    return None
