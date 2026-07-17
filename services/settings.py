"""User settings service."""

from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlmodel import Session, select
from fastapi import HTTPException

from models import UserSettings, UserAIProvider, CryptoAccount
from dtos.settings import (
    UserSettingsUpdate,
    UserSettingsResponse,
    AIProviderConfig,
    AIProviderUpdate,
    AIOptionsResponse,
    AIProviderOption,
)
from services.encryption import encrypt_data, decrypt_data, hash_index
from services.ai.registry import PROVIDER_REGISTRY, CAPABILITY_PRIORITY, provider_supports


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_ai_providers(session: Session, user_uuid_bidx: str) -> list[UserAIProvider]:
    """Return all AI provider rows for a user."""
    return list(
        session.exec(
            select(UserAIProvider).where(UserAIProvider.user_uuid_bidx == user_uuid_bidx)
        ).all()
    )


def _map_settings_to_response(
    settings: UserSettings,
    master_key: str,
    ai_providers: list[UserAIProvider],
) -> UserSettingsResponse:
    """Decrypt and map UserSettings + provider rows to response DTO."""
    objectives = None
    if settings.objectives_enc:
        objectives = decrypt_data(settings.objectives_enc, master_key)
        if objectives == "":
            objectives = None

    provider_configs = [
        AIProviderConfig(
            provider=p.provider,
            has_key=bool(p.api_key_enc),
            selected_model=p.selected_model,
        )
        for p in ai_providers
    ]

    return UserSettingsResponse(
        objectives=objectives,
        theme=settings.theme,
        display_timezone=settings.display_timezone,
        flat_tax_rate=float(settings.flat_tax_rate),
        tax_pea_rate=float(settings.tax_pea_rate),
        yield_expectation=float(settings.yield_expectation),
        inflation_rate=float(settings.inflation_rate),
        crypto_module_enabled=settings.crypto_module_enabled,
        crypto_mode=settings.crypto_mode,
        crypto_show_negative_positions=settings.crypto_show_negative_positions,
        bank_module_enabled=settings.bank_module_enabled,
        cashflow_module_enabled=settings.cashflow_module_enabled,
        wealth_module_enabled=settings.wealth_module_enabled,
        ai_feature_enabled=settings.ai_feature_enabled,
        ai_vision_provider=settings.ai_vision_provider,
        ai_chat_provider=settings.ai_chat_provider,
        ai_providers=provider_configs,
        usd_eur_rate=float(settings.usd_eur_rate) if settings.usd_eur_rate is not None else None,
        created_at=settings.created_at,
        updated_at=settings.updated_at,
    )


# ---------------------------------------------------------------------------
# Core settings CRUD
# ---------------------------------------------------------------------------

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
    user_bidx = hash_index(user_uuid, master_key)
    settings = get_or_create_settings(session, user_uuid, master_key)
    ai_providers = _get_ai_providers(session, user_bidx)
    return _map_settings_to_response(settings, master_key, ai_providers)


def update_settings(
    session: Session,
    user_uuid: str,
    master_key: str,
    data: UserSettingsUpdate,
) -> UserSettingsResponse:
    """Update user settings."""
    user_bidx = hash_index(user_uuid, master_key)
    settings = get_or_create_settings(session, user_uuid, master_key)

    # Validate SINGLE mode transition: max 1 account allowed
    if data.crypto_mode == "SINGLE":
        account_count = session.exec(
            select(CryptoAccount).where(CryptoAccount.user_uuid_bidx == user_bidx)
        ).all()

        if len(account_count) > 1:
            raise HTTPException(
                status_code=400,
                detail=f"Vous avez {len(account_count)} compte(s) crypto. Vous devez en avoir 0 ou 1 pour activer le mode Patrimoine Global. Supprimez d'abord les comptes excédentaires.",
            )

    if data.objectives is not None:
        settings.objectives_enc = encrypt_data(data.objectives, master_key)

    if data.theme is not None:
        settings.theme = data.theme

    if "display_timezone" in data.model_fields_set:
        tz = data.display_timezone
        if tz is not None:
            try:
                ZoneInfo(tz)
            except (KeyError, ValueError, ZoneInfoNotFoundError):
                raise HTTPException(status_code=400, detail=f"Fuseau horaire inconnu : '{tz}'.")
        settings.display_timezone = tz

    if data.flat_tax_rate is not None:
        settings.flat_tax_rate = Decimal(str(data.flat_tax_rate))

    if data.tax_pea_rate is not None:
        settings.tax_pea_rate = Decimal(str(data.tax_pea_rate))

    if data.yield_expectation is not None:
        settings.yield_expectation = Decimal(str(data.yield_expectation))

    if data.inflation_rate is not None:
        settings.inflation_rate = Decimal(str(data.inflation_rate))

    if data.crypto_module_enabled is not None:
        settings.crypto_module_enabled = data.crypto_module_enabled

    if data.crypto_mode is not None:
        if data.crypto_mode in ("SINGLE", "MULTI"):
            settings.crypto_mode = data.crypto_mode

    if data.crypto_show_negative_positions is not None:
        settings.crypto_show_negative_positions = data.crypto_show_negative_positions

    if data.bank_module_enabled is not None:
        settings.bank_module_enabled = data.bank_module_enabled

    if data.cashflow_module_enabled is not None:
        settings.cashflow_module_enabled = data.cashflow_module_enabled

    if data.wealth_module_enabled is not None:
        settings.wealth_module_enabled = data.wealth_module_enabled

    if data.ai_feature_enabled is not None:
        settings.ai_feature_enabled = data.ai_feature_enabled

    if "ai_vision_provider" in data.model_fields_set:
        p = data.ai_vision_provider
        if p is not None and not provider_supports(p, "vision"):
            raise HTTPException(
                status_code=400,
                detail=f"Le provider '{p}' ne supporte pas la vision.",
            )
        settings.ai_vision_provider = p

    if "ai_chat_provider" in data.model_fields_set:
        p = data.ai_chat_provider
        if p is not None and not provider_supports(p, "chat"):
            raise HTTPException(
                status_code=400,
                detail=f"Le provider '{p}' ne supporte pas le chat.",
            )
        settings.ai_chat_provider = p

    if "usd_eur_rate" in data.model_fields_set:
        if data.usd_eur_rate is not None:
            settings.usd_eur_rate = Decimal(str(data.usd_eur_rate))
        else:
            settings.usd_eur_rate = None

    session.add(settings)
    session.commit()
    session.refresh(settings)

    ai_providers = _get_ai_providers(session, user_bidx)
    return _map_settings_to_response(settings, master_key, ai_providers)


# ---------------------------------------------------------------------------
# AI Provider CRUD
# ---------------------------------------------------------------------------

def update_ai_provider(
    session: Session,
    user_uuid: str,
    master_key: str,
    provider: str,
    data: AIProviderUpdate,
) -> AIProviderConfig:
    """
    Upsert the API key and/or selected model for a specific AI provider.

    - api_key=None removes the key (and the row if no model is set either).
    - api_key="" also removes the key.
    - selected_model=None resets to provider default.
    """
    if provider not in PROVIDER_REGISTRY:
        raise HTTPException(status_code=404, detail=f"Provider '{provider}' non supporté.")

    user_bidx = hash_index(user_uuid, master_key)

    # Validate model if provided
    if data.selected_model is not None:
        valid_model_ids = [m["id"] for m in PROVIDER_REGISTRY[provider]["models"]]
        if data.selected_model not in valid_model_ids:
            raise HTTPException(
                status_code=400,
                detail=f"Modèle '{data.selected_model}' non valide pour le provider '{provider}'.",
            )

    # Find or create the provider row
    row = session.exec(
        select(UserAIProvider).where(
            UserAIProvider.user_uuid_bidx == user_bidx,
            UserAIProvider.provider == provider,
        )
    ).first()

    # Compute new encrypted key
    new_key_enc: str | None
    if "api_key" in data.model_fields_set:
        if data.api_key and data.api_key.strip():
            new_key_enc = encrypt_data(data.api_key.strip(), master_key)
        else:
            new_key_enc = None
    else:
        # Field not supplied — keep existing
        new_key_enc = row.api_key_enc if row else None

    new_model: str | None
    if "selected_model" in data.model_fields_set:
        new_model = data.selected_model
    else:
        new_model = row.selected_model if row else None

    if row is None:
        row = UserAIProvider(
            user_uuid_bidx=user_bidx,
            provider=provider,
            api_key_enc=new_key_enc,
            selected_model=new_model,
        )
    else:
        row.api_key_enc = new_key_enc
        row.selected_model = new_model

    session.add(row)
    session.commit()
    session.refresh(row)

    return AIProviderConfig(
        provider=row.provider,
        has_key=bool(row.api_key_enc),
        selected_model=row.selected_model,
    )


def get_ai_options(
    session: Session,
    user_uuid: str,
    master_key: str,
) -> AIOptionsResponse:
    """
    Return provider options per capability, annotated with whether the user
    has configured an API key for each provider.
    """
    user_bidx = hash_index(user_uuid, master_key)
    ai_providers = _get_ai_providers(session, user_bidx)
    key_map = {p.provider: bool(p.api_key_enc) for p in ai_providers}
    model_map = {p.provider: p.selected_model for p in ai_providers}

    capabilities: dict[str, list[AIProviderOption]] = {}
    for capability, priority in CAPABILITY_PRIORITY.items():
        options = []
        for provider_id in priority:
            entry = PROVIDER_REGISTRY[provider_id]
            options.append(
                AIProviderOption(
                    provider=provider_id,
                    label=entry["label"],
                    has_key=key_map.get(provider_id, False),
                    models=entry["models"],
                )
            )
        capabilities[capability] = options

    return AIOptionsResponse(capabilities=capabilities)
