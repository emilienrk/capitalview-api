"""Placement routes: AV, PER, SCPI and the like, followed by hand."""

from datetime import date, datetime, timezone
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlmodel import Session

from database import get_session
from dtos.placement import (
    PlacementAccountCreate,
    PlacementAccountResponse,
    PlacementAccountUpdate,
    PlacementEntryCreate,
    PlacementEntryResponse,
    PlacementEntryUpdate,
    PlacementSummaryResponse,
)
from dtos.transaction import AccountHistorySnapshotResponse
from models import User
from models.placement import PlacementAccount
from services.account_history import rebuild_account_history_from_date
from services.auth import get_current_user, get_master_key
from services.encryption import hash_index
from services.placement import (
    build_timeline,
    create_account,
    create_entry,
    delete_account,
    delete_entry,
    entry_day,
    get_account,
    get_all_placements_history,
    get_placement_account_history,
    get_owned_account,
    get_owned_entry,
    get_user_placements,
    list_entries,
    update_account,
    update_entry,
)

router = APIRouter(prefix="/placements", tags=["Placements"])


def _owned_or_404(session: Session, account_id: str, user: User, master_key: str) -> PlacementAccount:
    account = get_owned_account(session, account_id, user.uuid, master_key)
    if account is None:
        raise HTTPException(status_code=404, detail="Placement introuvable")
    return account


def _schedule_rebuild(
    background_tasks: BackgroundTasks,
    session: Session,
    account: PlacementAccount,
    user_uuid: str,
    master_key: str,
    *affected: date | None,
) -> None:
    """Rebuild the placement's history from the earliest day a change can reach.

    A balance reshapes the whole span back to the previous one, and a flow the
    span it falls in: rebuilding from the placement's first entry covers both,
    and costs nothing a market fetch would, since no price is involved.
    """
    start = build_timeline(session, account.uuid, master_key).start
    candidates = [d for d in (start, account.opened_at, *affected) if d is not None]
    from_date = min(candidates) if candidates else datetime.now(timezone.utc).date()
    background_tasks.add_task(
        rebuild_account_history_from_date,
        user_uuid,
        hash_index(account.uuid, master_key),
        from_date,
        master_key,
    )


@router.get("", response_model=PlacementSummaryResponse)
def list_placements(
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    return get_user_placements(session, current_user.uuid, master_key)


@router.post("", response_model=PlacementAccountResponse, status_code=201)
def create_placement(
    data: PlacementAccountCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    return create_account(session, data, current_user.uuid, master_key)


@router.get("/history", response_model=list[AccountHistorySnapshotResponse])
def history(
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    return get_all_placements_history(session, current_user.uuid, master_key)


@router.get("/{account_id}", response_model=PlacementAccountResponse)
def get_placement(
    account_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    return get_account(session, _owned_or_404(session, account_id, current_user, master_key), master_key)


@router.put("/{account_id}", response_model=PlacementAccountResponse)
def update_placement(
    account_id: str,
    data: PlacementAccountUpdate,
    background_tasks: BackgroundTasks,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    account = _owned_or_404(session, account_id, current_user, master_key)
    previous_opened_at = account.opened_at
    result = update_account(session, account, data, master_key)
    if "opened_at" in data.model_fields_set:
        _schedule_rebuild(
            background_tasks, session, account, current_user.uuid, master_key, previous_opened_at
        )
    return result


@router.delete("/{account_id}", status_code=204)
def delete_placement(
    account_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    delete_account(session, _owned_or_404(session, account_id, current_user, master_key), master_key)
    return None


@router.get("/{account_id}/history", response_model=list[AccountHistorySnapshotResponse])
def placement_history(
    account_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    account = _owned_or_404(session, account_id, current_user, master_key)
    return get_placement_account_history(session, account, master_key)


@router.get("/{account_id}/entries", response_model=list[PlacementEntryResponse])
def list_placement_entries(
    account_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    return list_entries(session, _owned_or_404(session, account_id, current_user, master_key), master_key)


@router.post("/{account_id}/entries", response_model=PlacementEntryResponse, status_code=201)
def add_entry(
    account_id: str,
    data: PlacementEntryCreate,
    background_tasks: BackgroundTasks,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    account = _owned_or_404(session, account_id, current_user, master_key)
    try:
        result = create_entry(session, account, data, master_key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _schedule_rebuild(background_tasks, session, account, current_user.uuid, master_key, data.occurred_at)
    return result


@router.put("/{account_id}/entries/{entry_id}", response_model=PlacementEntryResponse)
def edit_entry(
    account_id: str,
    entry_id: str,
    data: PlacementEntryUpdate,
    background_tasks: BackgroundTasks,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    account = _owned_or_404(session, account_id, current_user, master_key)
    entry = get_owned_entry(session, account, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Opération introuvable")
    previous_day = entry_day(entry, master_key)
    try:
        result = update_entry(session, entry, data, master_key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _schedule_rebuild(
        background_tasks, session, account, current_user.uuid, master_key, previous_day, result.occurred_at
    )
    return result


@router.delete("/{account_id}/entries/{entry_id}", status_code=204)
def remove_entry(
    account_id: str,
    entry_id: str,
    background_tasks: BackgroundTasks,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    account = _owned_or_404(session, account_id, current_user, master_key)
    entry = get_owned_entry(session, account, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Opération introuvable")
    # Read before the delete: the entry may have been the placement's first.
    previous_start = build_timeline(session, account.uuid, master_key).start
    try:
        delete_entry(session, entry, master_key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _schedule_rebuild(
        background_tasks, session, account, current_user.uuid, master_key, previous_start
    )
    return None
