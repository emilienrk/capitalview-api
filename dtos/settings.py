"""User settings schemas."""

from pydantic import BaseModel, Field
from datetime import datetime


# ---------------------------------------------------------------------------
# AI Provider DTOs
# ---------------------------------------------------------------------------

class AIProviderConfig(BaseModel):
    """State of a single AI provider for a user (read-only)."""
    provider: str               # "google" | "anthropic" | "deepseek"
    has_key: bool               # True if an API key is configured
    selected_model: str | None  # None = provider default


class AIProviderUpdate(BaseModel):
    """Update the API key and/or model for a single provider."""
    api_key: str | None = None          # None = delete the key; omit field to leave unchanged
    selected_model: str | None = None   # None = use provider default


class AIProviderOption(BaseModel):
    """A selectable provider option returned by GET /settings/ai/options."""
    provider: str
    label: str
    has_key: bool
    models: list[dict]  # [{"id": str, "label": str, "default"?: bool}]


class AIOptionsResponse(BaseModel):
    """Response for GET /settings/ai/options."""
    capabilities: dict[str, list[AIProviderOption]]  # {"vision": [...], "chat": [...]}


# ---------------------------------------------------------------------------
# User Settings DTOs
# ---------------------------------------------------------------------------

class UserSettingsUpdate(BaseModel):
    """Update user settings (all fields optional)."""
    objectives: str | None = None
    theme: str | None = None
    display_timezone: str | None = None  # IANA name; None = follow the browser
    display_locale: str | None = None  # BCP 47 tag; None = app default (fr-FR)
    flat_tax_rate: float | None = Field(None, ge=0, le=1)
    tax_pea_rate: float | None = Field(None, ge=0, le=1)
    yield_expectation: float | None = Field(None, ge=0, le=1)
    inflation_rate: float | None = Field(None, ge=0, le=1)
    crypto_module_enabled: bool | None = None
    crypto_mode: str | None = None
    crypto_show_negative_positions: bool | None = None
    bank_module_enabled: bool | None = None
    cashflow_module_enabled: bool | None = None
    wealth_module_enabled: bool | None = None
    ai_feature_enabled: bool | None = None
    ai_vision_provider: str | None = None
    ai_chat_provider: str | None = None
    usd_eur_rate: float | None = Field(None, gt=0, le=10)


class UserSettingsResponse(BaseModel):
    """User settings response."""
    model_config = {"from_attributes": True}

    objectives: str | None = None
    theme: str = "system"
    display_timezone: str | None = None
    display_locale: str | None = None
    flat_tax_rate: float = 0.30
    tax_pea_rate: float = 0.172
    yield_expectation: float = 0.05
    inflation_rate: float = 0.02
    crypto_module_enabled: bool = False
    crypto_mode: str = "SINGLE"
    crypto_show_negative_positions: bool = False
    bank_module_enabled: bool = False
    cashflow_module_enabled: bool = True
    wealth_module_enabled: bool = False
    ai_feature_enabled: bool = False
    ai_vision_provider: str | None = None
    ai_chat_provider: str | None = None
    ai_providers: list[AIProviderConfig] = []  # configured providers with key state
    usd_eur_rate: float | None = None
    created_at: datetime
    updated_at: datetime
