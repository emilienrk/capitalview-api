"""
User and UserSettings models.
"""
from sqlmodel import SQLModel, Field
from sqlalchemy import Column, TEXT, UniqueConstraint
from datetime import datetime
from decimal import Decimal
import sqlalchemy as sa


class User(SQLModel, table=True):
    """Central user table."""
    __tablename__ = "users"
    __table_args__ = {"extend_existing": True}

    uuid: str = Field(default=None, primary_key=True)
    auth_salt: str = Field(sa_column=Column(TEXT, nullable=False))
    username: str = Field(nullable=False, unique=True, index=True)
    email: str = Field(nullable=False, unique=True, index=True)
    last_username_change: datetime | None = Field(default=None)
    last_email_change: datetime | None = Field(default=None)
    password_hash: str = Field(nullable=False)
    is_active: bool = Field(default=True, nullable=False)
    last_login: datetime | None = Field(default=None)
    # Wrapped Master Key: Enc(KEK, MK) where KEK = Argon2id(secret, salt).
    # Legacy accounts (columns NULL) derive the MK directly from the password
    # until the lazy migration wraps it at their next login.
    mk_wrapped_password: str | None = Field(default=None, sa_column=Column(TEXT))
    mk_salt_password: str | None = Field(default=None, sa_column=Column(TEXT))
    mk_wrapped_recovery: str | None = Field(default=None, sa_column=Column(TEXT))
    mk_salt_recovery: str | None = Field(default=None, sa_column=Column(TEXT))
    # TOTP secret encrypted with a server key (must be verifiable before the MK is released)
    totp_secret_enc: str | None = Field(default=None, sa_column=Column(TEXT))
    totp_enabled: bool = Field(default=False, nullable=False)
    totp_last_used_step: int | None = Field(default=None)
    created_at: datetime = Field(
        default=sa.func.now(),
        sa_column=Column(sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)
    )
    updated_at: datetime = Field(
        default=sa.func.now(),
        sa_column=Column(
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        )
    )


class UserAIProvider(SQLModel, table=True):
    """
    Per-provider AI configuration for a user.

    Stores the encrypted API key and the user's preferred model for each provider.
    One row per (user, provider) pair — adding a new provider never requires a migration.
    """
    __tablename__ = "user_ai_providers"
    __table_args__ = (
        UniqueConstraint("user_uuid_bidx", "provider", name="uq_user_ai_provider"),
        {"extend_existing": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    user_uuid_bidx: str = Field(index=True)
    provider: str = Field(nullable=False)          # e.g. "google" | "anthropic" | "deepseek"
    api_key_enc: str | None = Field(default=None, sa_column=Column(TEXT))
    selected_model: str | None = Field(default=None)  # None = use provider default model


class UserSettings(SQLModel, table=True):
    """Simulation constants per user (inflation, tax rates)."""
    __tablename__ = "user_settings"
    __table_args__ = {"extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    user_uuid_bidx: str = Field(index=True, unique=True)
    objectives_enc: str | None = Field(sa_column=Column(TEXT))
    theme: str = Field(default="system", nullable=False)
    # IANA timezone for date display (None = follow the browser)
    display_timezone: str | None = Field(default=None, nullable=True)
    # BCP 47 locale driving date/number formatting (None = app default, fr-FR)
    display_locale: str | None = Field(default=None, nullable=True)
    dashboard_layout_enc: str | None = Field(sa_column=Column(TEXT))
    flat_tax_rate: Decimal = Field(default=Decimal("0.30"), max_digits=5, decimal_places=4)
    tax_pea_rate: Decimal = Field(default=Decimal("0.172"), max_digits=5, decimal_places=4)
    yield_expectation: Decimal = Field(default=Decimal("0.05"), max_digits=5, decimal_places=4)
    inflation_rate: Decimal = Field(default=Decimal("0.02"), max_digits=5, decimal_places=4)
    crypto_module_enabled: bool = Field(default=False, nullable=False)
    crypto_mode: str = Field(default="SINGLE", nullable=False)
    crypto_show_negative_positions: bool = Field(default=False, nullable=False)
    bank_module_enabled: bool = Field(default=True, nullable=False)
    cashflow_module_enabled: bool = Field(default=True, nullable=False)
    wealth_module_enabled: bool = Field(default=True, nullable=False)
    ai_feature_enabled: bool = Field(default=False, nullable=False)
    # Preferred provider per capability (None = auto-select from priority list)
    ai_vision_provider: str | None = Field(default=None, nullable=True)
    ai_chat_provider: str | None = Field(default=None, nullable=True)
    # Manual USD→EUR rate override (None = use auto-fetched rate)
    usd_eur_rate: Decimal | None = Field(
        default=None,
        max_digits=10,
        decimal_places=6,
        sa_column=Column(sa.Numeric(10, 6), nullable=True),
    )

    created_at: datetime = Field(
        default=sa.func.now(),
        sa_column=Column(sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)
    )
    updated_at: datetime = Field(
        default=sa.func.now(),
        sa_column=Column(
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        )
    )


class TotpBackupCode(SQLModel, table=True):
    """Single-use 2FA backup codes, stored as HMAC-SHA256 hashes."""
    __tablename__ = "totp_backup_codes"
    __table_args__ = {"extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    user_uuid: str = Field(
        sa_column=Column(
            sa.String,
            sa.ForeignKey("users.uuid", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    code_hash: str = Field(nullable=False, index=True)
    used_at: datetime | None = Field(default=None)
    created_at: datetime = Field(
        default=sa.func.now(),
        sa_column=Column(sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)
    )


class RefreshToken(SQLModel, table=True):
    """Refresh tokens for JWT authentication."""
    __tablename__ = "refresh_tokens"
    __table_args__ = {"extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    user_uuid: str = Field(
        sa_column=Column(
            sa.String,
            sa.ForeignKey("users.uuid", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    token_hash: str = Field(nullable=False, unique=True, index=True)
    expires_at: datetime = Field(nullable=False)
    revoked: bool = Field(default=False, nullable=False)
    created_at: datetime = Field(
        default=sa.func.now(),
        sa_column=Column(sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)
    )
