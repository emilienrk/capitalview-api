"""Authentication schemas for API requests and responses."""

import re
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, field_validator, ConfigDict


def validate_password_strength(v: str) -> str:
    """Enforce password complexity rules (shared by register/change/recover)."""
    if not re.search(r'[A-Z]', v):
        raise ValueError('Le mot de passe doit contenir au moins une majuscule')
    if not re.search(r'[a-z]', v):
        raise ValueError('Le mot de passe doit contenir au moins une minuscule')
    if not re.search(r'\d', v):
        raise ValueError('Le mot de passe doit contenir au moins un chiffre')
    if not re.search(r'[^A-Za-z0-9]', v):
        raise ValueError('Le mot de passe doit contenir au moins un caractère spécial')
    return v


class RegisterRequest(BaseModel):
    """User registration request."""
    username: str = Field(..., min_length=3, max_length=50)
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=100)

    _validate_password = field_validator('password')(validate_password_strength)

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
    master_key: str | None = Field(
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
    totp_enabled: bool = False
    last_username_change: datetime | None = None
    last_email_change: datetime | None = None
    last_login: datetime | None = None
    created_at: datetime


class MessageResponse(BaseModel):
    """Generic message response."""
    message: str

class EmailUpdateRequest(BaseModel):
    """Email update request."""
    email: EmailStr


class UsernameUpdateRequest(BaseModel):
    """Username update request."""
    username: str = Field(..., min_length=3, max_length=50)

    @field_validator('username')
    @classmethod
    def validate_username(cls, v: str) -> str:
        """Only allow alphanumeric, underscores, and hyphens."""
        if not re.match(r'^[a-zA-Z0-9_-]+$', v):
            raise ValueError('Le nom d\'utilisateur ne peut contenir que des lettres, chiffres, _ et -')
        return v


class PasswordChangeRequest(BaseModel):
    """Password change request. TOTP code required when 2FA is enabled."""
    current_password: str
    new_password: str = Field(..., min_length=8, max_length=100)
    totp_code: str | None = None

    _validate_password = field_validator('new_password')(validate_password_strength)


class RecoveryKeyGenerateRequest(BaseModel):
    """Generate (or regenerate) the account recovery key."""
    password: str


class RecoveryKeyResponse(BaseModel):
    """The recovery key — shown once, never stored in plaintext."""
    recovery_key: str


class RecoverRequest(BaseModel):
    """Account recovery: reset the password using the recovery key."""
    email: EmailStr
    recovery_key: str
    new_password: str = Field(..., min_length=8, max_length=100)
    totp_code: str | None = None

    _validate_password = field_validator('new_password')(validate_password_strength)


class RecoverResponse(TokenResponse):
    """Recovery response: a fresh session plus the replacement recovery key.

    The used recovery key is single-use; ``new_recovery_key`` replaces it and
    is shown only once.
    """
    new_recovery_key: str


class TwoFARequiredResponse(BaseModel):
    """Login step-1 response when 2FA is enabled: complete via POST /auth/login/2fa."""
    two_fa_required: bool = True
    pending_token: str
    expires_in: int


class Login2FARequest(BaseModel):
    """Login step 2: the pending token from step 1 plus a TOTP or backup code."""
    pending_token: str
    code: str


class TwoFASetupRequest(BaseModel):
    """Start 2FA setup (password confirmation required)."""
    password: str


class TwoFASetupResponse(BaseModel):
    """Pending 2FA setup: secret + provisioning URI (rendered as a QR by the frontend)."""
    secret: str
    otpauth_uri: str


class TwoFAEnableRequest(BaseModel):
    """Confirm 2FA activation with a first valid TOTP code."""
    code: str


class TwoFAEnableResponse(BaseModel):
    """2FA activated: single-use backup codes, shown once."""
    backup_codes: list[str]


class TwoFADisableRequest(BaseModel):
    """Disable 2FA (password + TOTP/backup code required)."""
    password: str
    code: str
