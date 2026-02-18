"""User settings service."""

from decimal import Decimal
from typing import Optional
from sqlmodel import Session, select

from models import UserSettings
from dtos.settings import UserSettingsUpdate, UserSettingsResponse
from services.encryption import encrypt_data, decrypt_data, hash_index


def _map_settings_to_response(settings: UserSettings, master_key: str) -> UserSettingsResponse:
    """Decrypt and map UserSettings to response DTO."""
    objectives = None
    if settings.objectives_enc:
        objectives = decrypt_data(settings.objectives_enc, master_key)
        if objectives == "":
            objectives = None

    return UserSettingsResponse(
        objectives=objectives,
        theme=settings.theme,
        flat_tax_rate=float(settings.flat_tax_rate),
        tax_pea_rate=float(settings.tax_pea_rate),
        yield_expectation=float(settings.yield_expectation),
        inflation_rate=float(settings.inflation_rate),
        created_at=settings.created_at,
        updated_at=settings.updated_at,
    )


def get_or_create_settings(
    session: Session,
    user_uuid: str,
    master_key: str,
) -> UserSettings:
    """Get existing settings or create defaults for user."""
    user_bidx = hash_index(user_uuid, master_key)
    statement = select(UserSettings).where(UserSettings.user_uuid_bidx == user_bidx)
    settings = session.exec(statement).first()

    if not settings:
        settings = UserSettings(user_uuid_bidx=user_bidx)
        session.add(settings)
        session.commit()
        session.refresh(settings)

    return settings


def get_settings(
    session: Session,
    user_uuid: str,
    master_key: str,
) -> UserSettingsResponse:
    """Get user settings (creates defaults if needed)."""
    settings = get_or_create_settings(session, user_uuid, master_key)
    return _map_settings_to_response(settings, master_key)


def update_settings(
    session: Session,
    user_uuid: str,
    master_key: str,
    data: UserSettingsUpdate,
) -> UserSettingsResponse:
    """Update user settings."""
    settings = get_or_create_settings(session, user_uuid, master_key)

    if data.objectives is not None:
        settings.objectives_enc = encrypt_data(data.objectives, master_key)

    if data.theme is not None:
        settings.theme = data.theme

    if data.flat_tax_rate is not None:
        settings.flat_tax_rate = Decimal(str(data.flat_tax_rate))

    if data.tax_pea_rate is not None:
        settings.tax_pea_rate = Decimal(str(data.tax_pea_rate))

    if data.yield_expectation is not None:
        settings.yield_expectation = Decimal(str(data.yield_expectation))

    if data.inflation_rate is not None:
        settings.inflation_rate = Decimal(str(data.inflation_rate))

    session.add(settings)
    session.commit()
    session.refresh(settings)

    return _map_settings_to_response(settings, master_key)
