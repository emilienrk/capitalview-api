"""User settings routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, Path
from sqlmodel import Session

from database import get_session
from models import User
from services.auth import get_current_user, get_master_key
from dtos.settings import (
    UserSettingsUpdate,
    UserSettingsResponse,
    AIProviderUpdate,
    AIProviderConfig,
    AIOptionsResponse,
)
from services.settings import get_settings, update_settings, update_ai_provider, get_ai_options

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


@router.get("/ai/options", response_model=AIOptionsResponse)
def get_ai_provider_options(
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """
    Return supported AI providers per capability (vision, chat),
    annotated with whether the user has configured an API key for each.
    """
    return get_ai_options(session, current_user.uuid, master_key)


@router.put("/ai/providers/{provider}", response_model=AIProviderConfig)
def update_ai_provider_settings(
    data: AIProviderUpdate,
    provider: Annotated[str, Path(description="Provider ID: google | anthropic | deepseek")],
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """
    Update the API key and/or selected model for a specific AI provider.
    Pass api_key=null to remove the key.
    Pass selected_model=null to reset to the provider's default model.
    """
    return update_ai_provider(session, current_user.uuid, master_key, provider, data)
