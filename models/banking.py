"""
Banking models: user credentials, linking flow, and active consents.

BYO Enable Banking credentials: each user brings their own application_id and
private key, since Enable Banking's free tier only exposes accounts linked by
the account holder themselves (see services/banking/credentials.py).
"""
from datetime import date, datetime
from sqlmodel import SQLModel, Field
from sqlalchemy import Column, TEXT
import sqlalchemy as sa
import uuid


class UserBankConnection(SQLModel, table=True):
    """One Enable Banking application per user."""
    __tablename__ = "user_bank_connections"
    __table_args__ = {"extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    user_uuid_bidx: str = Field(sa_column=Column(TEXT, nullable=False, unique=True, index=True))
    application_id_enc: str | None = Field(default=None, sa_column=Column(TEXT))
    private_key_enc: str | None = Field(default=None, sa_column=Column(TEXT))

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


class BankAuthorization(SQLModel, table=True):
    """An in-progress linking flow: created when the user opens the bank's
    consent page, consumed on callback. Ephemeral by design (see expires_at).
    """
    __tablename__ = "bank_authorizations"
    __table_args__ = {"extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    user_uuid_bidx: str = Field(sa_column=Column(TEXT, nullable=False, index=True))
    # hash_index(state, master_key) — recovers the row on callback without ever
    # storing the OAuth-style `state` in clear.
    state_bidx: str = Field(sa_column=Column(TEXT, nullable=False, unique=True, index=True))
    aspsp_name_enc: str | None = Field(default=None, sa_column=Column(TEXT))
    aspsp_country_enc: str | None = Field(default=None, sa_column=Column(TEXT))
    authorization_id_enc: str | None = Field(default=None, sa_column=Column(TEXT))

    created_at: datetime = Field(
        default=sa.func.now(),
        sa_column=Column(sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)
    )
    # Cutoff for purging abandoned flows; set by the caller from the flow's own TTL.
    expires_at: datetime = Field(sa_column=Column(sa.DateTime(timezone=True), nullable=False))


class BankSession(SQLModel, table=True):
    """An active Enable Banking consent. A user may hold several, one per bank."""
    __tablename__ = "bank_sessions"
    __table_args__ = {"extend_existing": True}

    uuid: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    user_uuid_bidx: str = Field(sa_column=Column(TEXT, nullable=False, index=True))
    session_id_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    aspsp_name_enc: str | None = Field(default=None, sa_column=Column(TEXT))
    aspsp_country_enc: str | None = Field(default=None, sa_column=Column(TEXT))
    # Deliberately clear text: operational metadata a Master-Key-less background
    # job needs to notify consent expiry. One of SessionStatus's eight values.
    status: str = Field(sa_column=Column(TEXT, nullable=False))
    consent_valid_until: datetime = Field(sa_column=Column(sa.DateTime(timezone=True), nullable=False))
    authorized_at: datetime = Field(sa_column=Column(sa.DateTime(timezone=True), nullable=False))

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


class BankAccountLink(SQLModel, table=True):
    """Attaches a CapitalView bank account to an Enable Banking account.

    Created at the rattachement step (never at the OAuth callback): a link
    requires the CapitalView bank_accounts.uuid it points to, which must
    already exist (bank_account_uuid_bidx is unique).

    account_uid_enc is disposable — Enable Banking's `uid` expires with the
    session and changes on every reconnection. identification_hash_bidx is the
    durable attachment key that survives reconnection.
    """
    __tablename__ = "bank_account_links"
    __table_args__ = {"extend_existing": True}

    uuid: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    user_uuid_bidx: str = Field(sa_column=Column(TEXT, nullable=False, index=True))
    bank_account_uuid_bidx: str = Field(sa_column=Column(TEXT, nullable=False, unique=True, index=True))
    # RESTRICT, not CASCADE: a session is a rotating, disposable credential, not
    # the link's owner. §B5 requires the link to survive session loss (reconnect
    # updates session_uuid in place); CASCADE would silently destroy anchor_date/
    # anchor_balance/identification_hash_bidx the moment a session row is deleted.
    session_uuid: str = Field(
        sa_column=Column(TEXT, sa.ForeignKey("bank_sessions.uuid", ondelete="RESTRICT"), nullable=False, index=True)
    )
    identification_hash_bidx: str = Field(sa_column=Column(TEXT, nullable=False, index=True))
    account_uid_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    # Deliberately clear text (with last_synced_at): the date of the last real
    # balance reading. "Estimated" markers are derived from this, never stored.
    anchor_date: date = Field(sa_column=Column(sa.Date, nullable=False))
    anchor_balance_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    # Deliberately clear text: caps sync to once a day, server-side.
    last_synced_at: date = Field(sa_column=Column(sa.Date, nullable=False))
    # NULL = no gap found at the last reconciliation check.
    last_reconciliation_gap_enc: str | None = Field(default=None, sa_column=Column(TEXT))

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
