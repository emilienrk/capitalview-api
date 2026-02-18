"""User settings schemas."""

from typing import Optional
from pydantic import BaseModel, Field
from datetime import datetime


class UserSettingsUpdate(BaseModel):
    """Update user settings (all fields optional)."""
    objectives: Optional[str] = None
    theme: Optional[str] = None
    flat_tax_rate: Optional[float] = Field(None, ge=0, le=1)
    tax_pea_rate: Optional[float] = Field(None, ge=0, le=1)
    yield_expectation: Optional[float] = Field(None, ge=0, le=1)
    inflation_rate: Optional[float] = Field(None, ge=0, le=1)


class UserSettingsResponse(BaseModel):
    """User settings response."""
    model_config = {"from_attributes": True}

    objectives: Optional[str] = None
    theme: str = "system"
    flat_tax_rate: float = 0.30
    tax_pea_rate: float = 0.172
    yield_expectation: float = 0.05
    inflation_rate: float = 0.02
    created_at: datetime
    updated_at: datetime
