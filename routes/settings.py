"""User settings routes."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlmodel import Session

from database import get_session
from models import User
from services.auth import get_current_user, get_master_key
from dtos.settings import UserSettingsUpdate, UserSettingsResponse
from services.settings import get_settings, update_settings

router = APIRouter(prefix="/settings", tags=["Settings"])


@router.get("", response_model=UserSettingsResponse)
def get_user_settings(
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """Get current user settings."""
    return get_settings(session, current_user.uuid, master_key)


@router.put("", response_model=UserSettingsResponse)
def update_user_settings(
    data: UserSettingsUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """Update current user settings."""
    return update_settings(session, current_user.uuid, master_key, data)
