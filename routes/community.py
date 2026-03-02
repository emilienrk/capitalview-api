"""Community view routes.

All endpoints require JWT authentication.
"""

from typing import Annotated, List

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from database import get_session
from models import User
from services.auth import get_current_user, get_master_key
from dtos.community import (
    AvailablePositionsResponse,
    CommunityProfileListItem,
    CommunityProfileResponse,
    CommunitySettingsResponse,
    CommunitySettingsUpdate,
)
from services.community import (
    get_available_positions,
    get_community_settings,
    get_public_profile,
    list_active_profiles,
    update_community_settings,
)

router = APIRouter(prefix="/community", tags=["Community"])


@router.get("/profiles", response_model=List[CommunityProfileListItem])
def list_profiles(
    current_user: Annotated[User, Depends(get_current_user)],
    session: Session = Depends(get_session),
):
    """List all active community profiles (usernames)."""
    return list_active_profiles(session)


@router.get("/profiles/{username}", response_model=CommunityProfileResponse)
def get_profile(
    username: str,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Session = Depends(get_session),
):
    """View a user's public community profile (PnL % only)."""
    profile = get_public_profile(session, username)
    if not profile:
        raise HTTPException(status_code=404, detail="Profil communautaire introuvable ou inactif.")
    return profile


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
    """Configure community profile — enable/disable and select shared assets.

    Requires the Master Key because the server must decrypt the user's
    transactions to compute PRU before re-encrypting with the community key.
    """
    return update_community_settings(session, current_user.uuid, master_key, data)


@router.get("/available-positions", response_model=AvailablePositionsResponse)
def get_available(
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """List all user positions eligible for community sharing.

    Returns only positions with a strictly positive amount.
    Requires Master Key to decrypt transactions.
    """
    return get_available_positions(session, current_user.uuid, master_key)
