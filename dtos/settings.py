"""User settings schemas."""

from pydantic import BaseModel, Field
from datetime import datetime


class UserSettingsUpdate(BaseModel):
    """Update user settings (all fields optional)."""
    objectives: str | None = None
    theme: str | None = None
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
    usd_eur_rate: float | None = Field(None, gt=0, le=10)


class UserSettingsResponse(BaseModel):
    """User settings response."""
    model_config = {"from_attributes": True}

    objectives: str | None = None
    theme: str = "system"
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
    usd_eur_rate: float | None = None
    created_at: datetime
    updated_at: datetime
