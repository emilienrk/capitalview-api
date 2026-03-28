"""Community view routes.

All endpoints require JWT authentication.

Search & privacy rules:
- GET /community/search?q=... : searches profiles (public=partial, private=exact match)
- GET /community/profiles : lists public profiles only
- GET /community/profiles/{username} : view profile (positions hidden if private + not mutual)
- POST /community/follow/{username} : follow a user
- DELETE /community/follow/{username} : unfollow a user
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from database import get_session
from models import User
from services.auth import get_current_user, get_master_key
from dtos.community import (
    AvailablePositionsResponse,
    CommunityProfileListItem,
    CommunityProfileResponse,
    CommunitySearchResult,
    CommunitySettingsResponse,
    CommunitySettingsUpdate,
    FollowResponse,
    PickCreate,
    PickResponse,
    PickUpdate,
)
from services.community import (
    create_pick,
    delete_pick,
    follow_user,
    get_available_positions,
    get_community_settings,
    get_public_profile,
    get_user_picks,
    list_active_profiles,
    search_profiles,
    unfollow_user,
    update_community_settings,
    update_pick,
)

router = APIRouter(prefix="/community", tags=["Community"])


@router.get("/search", response_model=list[CommunitySearchResult])
def search(
    q: str,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Session = Depends(get_session),
):
    """Search community profiles by username.

    Public profiles: partial match.
    Private profiles: exact username match only.
    """
    return search_profiles(session, q, current_user.uuid)


@router.get("/profiles", response_model=list[CommunityProfileListItem])
def list_profiles(
    current_user: Annotated[User, Depends(get_current_user)],
    session: Session = Depends(get_session),
):
    """List all active PUBLIC community profiles."""
    return list_active_profiles(session, current_user.uuid)


@router.get("/profiles/{username}", response_model=CommunityProfileResponse)
def get_profile(
    username: str,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Session = Depends(get_session),
):
    """View a user's community profile.

    If the profile is private and users are not mutual followers,
    positions are hidden (empty list, no PnL).
    """
    profile = get_public_profile(session, username, current_user.uuid)
    if not profile:
        raise HTTPException(status_code=404, detail="Profil communautaire introuvable ou inactif.")
    return profile


@router.post("/follow/{username}", response_model=FollowResponse)
def follow(
    username: str,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Session = Depends(get_session),
):
    """Follow a user."""
    try:
        return follow_user(session, current_user.uuid, username)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/follow/{username}", response_model=FollowResponse)
def unfollow(
    username: str,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Session = Depends(get_session),
):
    """Unfollow a user."""
    try:
        return unfollow_user(session, current_user.uuid, username)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/settings", response_model=CommunitySettingsResponse)
def get_settings(
    current_user: Annotated[User, Depends(get_current_user)],
    session: Session = Depends(get_session),
):
    """Get current community sharing settings."""
    return get_community_settings(session, current_user.uuid)


@router.put("/settings", response_model=CommunitySettingsResponse)
def update_settings(
    data: CommunitySettingsUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """Configure community profile — enable/disable and select shared assets."""
    return update_community_settings(session, current_user.uuid, master_key, data)


@router.get("/available-positions", response_model=AvailablePositionsResponse)
def get_available(
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """List all user positions eligible for community sharing."""
    return get_available_positions(session, current_user.uuid, master_key)


# ── Picks (likes) ──────────────────────────────────────────────

@router.get("/picks/me", response_model=list[PickResponse])
def my_picks(
    current_user: Annotated[User, Depends(get_current_user)],
    session: Session = Depends(get_session),
):
    """Get all picks for the current user."""
    return get_user_picks(session, current_user.uuid)


@router.post("/picks", response_model=PickResponse, status_code=201)
def add_pick(
    data: PickCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Session = Depends(get_session),
):
    """Like / pick a stock or crypto asset."""
    try:
        return create_pick(session, current_user.uuid, data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.put("/picks/{pick_id}", response_model=PickResponse)
def edit_pick(
    pick_id: int,
    data: PickUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Session = Depends(get_session),
):
    """Update comment / target price on a pick."""
    try:
        return update_pick(session, current_user.uuid, pick_id, data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except PermissionError:
        raise HTTPException(status_code=403, detail="Non autorisé.")


@router.delete("/picks/{pick_id}", status_code=204)
def remove_pick(
    pick_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Session = Depends(get_session),
):
    """Remove a pick."""
    try:
        delete_pick(session, current_user.uuid, pick_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except PermissionError:
        raise HTTPException(status_code=403, detail="Non autorisé.")
