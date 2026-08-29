"""
Banking models: user credentials, linking flow, and active consents.

BYO Enable Banking credentials: each user brings their own application_id and
private key, since Enable Banking's free tier only exposes accounts linked by
the account holder themselves (see services/banking/credentials.py).
"""
from datetime import date, datetime
from sqlmodel import SQLModel, Field
from sqlalchemy import Column, TEXT, UniqueConstraint
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
    # The accounts payload of POST /sessions, JSON then encrypted (same pattern
    # as AccountHistory.positions_enc). Written once, at the callback: the later
    # GET /sessions/{id} returns only uid + identification hashes, so every
    # human-readable attribute of a discovered account is delivered exactly
    # once. Read as a whole block, never queried field by field — no blind index.
    accounts_enc: str | None = Field(default=None, sa_column=Column(TEXT))

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


class BankTransaction(SQLModel, table=True):
    """A movement observed on a linked bank account.

    No date is ever stored in clear (see services/banking/transactions.py for
    how the blind indexes are built): period_bidx carries the "YYYY-MM" of the
    retained date so a month can be fetched by equality, dedup_bidx carries the
    (date, amount, currency, direction) fingerprint that catches the card /
    current-account duplication — §A5 spells out a triple, the currency was
    added to it by ruling R11 — and entry_ref_bidx carries the ASPSP's own
    entry_reference.

    The composite unique key is (account_id_bidx, entry_ref_bidx), never the
    reference alone: entry_reference is explicitly not globally unique, so two
    accounts may legitimately reuse one. It is nullable — the reference is
    optional at the contract, and reference-less transactions fall back on
    dedup_bidx.
    """
    __tablename__ = "bank_transactions"
    __table_args__ = (
        UniqueConstraint("account_id_bidx", "entry_ref_bidx", name="uq_bank_transactions_account_entry_ref"),
        {"extend_existing": True},
    )

    uuid: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        sa_column=Column(TEXT, primary_key=True, nullable=False),
    )
    account_id_bidx: str = Field(sa_column=Column(TEXT, nullable=False, index=True))
    period_bidx: str = Field(sa_column=Column(TEXT, nullable=False, index=True))
    # Not indexed on its own: it is only ever looked up alongside the account,
    # which the unique constraint above already covers.
    entry_ref_bidx: str | None = Field(default=None, sa_column=Column(TEXT))
    dedup_bidx: str = Field(sa_column=Column(TEXT, nullable=False, index=True))
    amount_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    # Currency of the operation as the bank reported it. A foreign currency
    # arrives without an exchange rate, so it is stored unconverted and readers
    # must check this before summing.
    currency_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    credit_debit_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    status_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    # The three dates as provided; any of them may be absent.
    booking_date_enc: str | None = Field(default=None, sa_column=Column(TEXT))
    value_date_enc: str | None = Field(default=None, sa_column=Column(TEXT))
    transaction_date_enc: str | None = Field(default=None, sa_column=Column(TEXT))
    remittance_enc: str | None = Field(default=None, sa_column=Column(TEXT))

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
