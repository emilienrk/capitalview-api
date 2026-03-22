"""Personal assets CRUD routes."""

from datetime import date, datetime, timezone
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
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
    get_asset_portfolio_history,
    get_asset_rebuild_start_date,
    get_asset_acquired_at,
)
from services.account_history import rebuild_account_history_from_date
from services.encryption import hash_index
from dtos.transaction import AccountHistorySnapshotResponse

router = APIRouter(prefix="/assets", tags=["Assets"])


def _schedule_asset_history_rebuild(
    background_tasks: BackgroundTasks,
    user_uuid: str,
    master_key: str,
    from_date,
) -> None:
    """Schedule a retroactive history rebuild for the user's ASSET virtual account."""
    user_uuid_bidx = hash_index(user_uuid, master_key)
    virtual_account_id = f"ASSET_PORTFOLIO::{user_uuid_bidx}"
    account_id_bidx = hash_index(virtual_account_id, master_key)
    background_tasks.add_task(
        rebuild_account_history_from_date,
        user_uuid,
        account_id_bidx,
        from_date,
        master_key,
    )


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


@router.get("/history", response_model=list[AccountHistorySnapshotResponse])
def get_portfolio_history(
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """Get historical daily snapshots for the user's asset portfolio."""
    return get_asset_portfolio_history(session, current_user.uuid, master_key)


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
    background_tasks: BackgroundTasks,
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

    old_acquired_at = get_asset_acquired_at(asset_model, master_key)

    result = update_asset(session, asset_model, data, master_key)

    rebuild_from: date | None = None

    if data.acquisition_date is not None:
        try:
            new_acquired_at = datetime.fromisoformat(
                data.acquisition_date.replace("Z", "+00:00")
            ).date()
        except Exception:
            new_acquired_at = old_acquired_at
        rebuild_from = min(old_acquired_at, new_acquired_at)

    if data.purchase_price is not None:
        rebuild_from = min(rebuild_from, old_acquired_at) if rebuild_from else old_acquired_at

    if data.estimated_value is not None:
        anchor_date = get_asset_rebuild_start_date(
            session, asset_id, datetime.now(timezone.utc).date(), master_key
        )
        rebuild_from = min(rebuild_from, anchor_date) if rebuild_from else anchor_date

    if rebuild_from is not None:
        _schedule_asset_history_rebuild(background_tasks, current_user.uuid, master_key, rebuild_from)

    return result


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
    background_tasks: BackgroundTasks,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """Add a new valuation entry for an asset."""
    existing = get_asset(session, asset_id, current_user.uuid, master_key)
    if not existing:
        raise HTTPException(status_code=404, detail="Asset not found")

    # Determine rebuild start BEFORE creating the new valuation (so the new
    # entry is not yet in the DB when we search for the previous one)
    try:
        val_date = datetime.fromisoformat(data.valued_at.replace("Z", "+00:00")).date()
    except Exception:
        val_date = datetime.utcnow().date()

    from_date = get_asset_rebuild_start_date(session, asset_id, val_date, master_key)

    result = create_valuation(session, asset_id, data, master_key)

    _schedule_asset_history_rebuild(background_tasks, current_user.uuid, master_key, from_date)

    return result


@router.delete("/{asset_id}/valuations/{valuation_id}", status_code=204)
def remove_valuation(
    asset_id: str,
    valuation_id: str,
    background_tasks: BackgroundTasks,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """Delete a valuation entry."""
    existing = get_asset(session, asset_id, current_user.uuid, master_key)
    if not existing:
        raise HTTPException(status_code=404, detail="Asset not found")

    # Capture the valuation date BEFORE deleting it so we can find the right from_date
    from models.asset import AssetValuation as AssetValuationModel
    v = session.get(AssetValuationModel, valuation_id)
    if not v:
        raise HTTPException(status_code=404, detail="Valuation not found")

    try:
        from services.encryption import decrypt_data as _dec
        val_date_raw = _dec(v.valued_at_enc, master_key)
        val_date = datetime.fromisoformat(val_date_raw.replace("Z", "+00:00")).date()
    except Exception:
        val_date = datetime.now(timezone.utc).date()

    from_date = get_asset_rebuild_start_date(session, asset_id, val_date, master_key)

    if not service_delete_valuation(session, valuation_id):
        raise HTTPException(status_code=404, detail="Valuation not found")

    _schedule_asset_history_rebuild(background_tasks, current_user.uuid, master_key, from_date)

    return None
