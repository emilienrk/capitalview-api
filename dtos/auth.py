"""Authentication schemas for API requests and responses."""

import re
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, Field, field_validator, ConfigDict


class RegisterRequest(BaseModel):
    """User registration request."""
    username: str = Field(..., min_length=3, max_length=50)
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=100)

    @field_validator('password')
    @classmethod
    def validate_password_strength(cls, v: str) -> str:
        """Enforce password complexity rules."""
        if not re.search(r'[A-Z]', v):
            raise ValueError('Le mot de passe doit contenir au moins une majuscule')
        if not re.search(r'[a-z]', v):
            raise ValueError('Le mot de passe doit contenir au moins une minuscule')
        if not re.search(r'\d', v):
            raise ValueError('Le mot de passe doit contenir au moins un chiffre')
        if not re.search(r'[^A-Za-z0-9]', v):
            raise ValueError('Le mot de passe doit contenir au moins un caractère spécial')
        return v

    @field_validator('username')
    @classmethod
    def validate_username(cls, v: str) -> str:
        """Only allow alphanumeric, underscores, and hyphens."""
        if not re.match(r'^[a-zA-Z0-9_-]+$', v):
            raise ValueError('Le nom d\'utilisateur ne peut contenir que des lettres, chiffres, _ et -')
        return v


class LoginRequest(BaseModel):
    """User login request."""
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    """JWT token response.

    Returned by ``/auth/login`` and ``/auth/register``.
    The ``master_key`` field is only present on login/register (not on refresh)
    so that automation clients (n8n, scripts) can capture it and pass it back
    via the ``X-Master-Key`` header on subsequent requests.
    """
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    master_key: Optional[str] = Field(
        default=None,
        description="Base64-encoded Master Key for data encryption. "
                    "Only returned on login/register. Use via X-Master-Key header for automation."
    )


class UserResponse(BaseModel):
    """User information response."""
    model_config = ConfigDict(from_attributes=True)
    
    username: str
    email: str
    is_active: bool
    last_login: Optional[datetime] = None
    created_at: datetime


class MessageResponse(BaseModel):
    """Generic message response."""
    message: str
