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
    # The day the bank was last called for this link, whatever the outcome. The
    # daily cap reads it alongside last_synced_at, which only a success moves —
    # otherwise a failing account called the bank again on every page render.
    # Clear text for the same reason as last_synced_at. NULL = no recorded attempt.
    last_sync_attempt_at: date | None = Field(default=None, sa_column=Column(sa.Date))
    # Why that attempt failed, as shown to the user. NULL once a sync succeeds.
    last_sync_error_enc: str | None = Field(default=None, sa_column=Column(TEXT))
    # NULL = no gap found at the last reconciliation check.
    last_reconciliation_gap_enc: str | None = Field(default=None, sa_column=Column(TEXT))
    # The balance readings previous syncs anchored on, as a JSON list of
    # {"d": "YYYY-MM-DD", "b": "123.45"} — the same pair as (anchor_date,
    # anchor_balance), kept for a few weeks.
    #
    # The check needs a reading old enough that the bank's own publication delay
    # has resolved: a balance counts an operation up to a day or two before the
    # transaction feed lists it, so comparing against yesterday's reading
    # reports a gap on a healthy account, then the opposite gap once the
    # operation lands. Encrypted: balances and dates never sit in clear (§A5).
    balance_checkpoints_enc: str | None = Field(default=None, sa_column=Column(TEXT))
    # Which balance type the last sync could read (CLBD, OTHR or ITAV). Clear
    # text, like anchor_date: it is a property of the bank's API, not of the
    # user. Stored rather than re-derived because everything downstream — the
    # reconciliation verdict, the wording the front shows — depends on whether
    # the curve rests on an accounting balance or on an available one, and the
    # balances payload is only in hand during a sync. NULL = never read.
    last_balance_type: str | None = Field(default=None, sa_column=Column(TEXT))
    # Whether the long history fetch has ever actually brought anything back.
    # Explicit rather than derived from `last_synced_at < anchor_date`: that
    # comparison was consumed by the first sync whether or not it returned a
    # single operation, and the years the bank still held were then never asked
    # for again — a flat curve, and nothing to retry it.
    history_seeded: bool = Field(
        default=False,
        sa_column=Column(sa.Boolean, nullable=False, server_default=sa.false()),
    )
    # The oldest operation date a seeding pass has ever brought back: how far the
    # bank actually serves this account's history, measured rather than assumed.
    # Some banks cap it — Revolut at ninety days once the consent is minutes
    # old — and without it the front could only promise a fuller history the
    # bank will never send. Encrypted: an operation date, never in clear (§A5).
    # Seeding passes only, and only ever widened: an incremental window says
    # nothing about how far back the bank goes. NULL = never measured.
    history_served_from_enc: str | None = Field(default=None, sa_column=Column(TEXT))

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
    # Blind index of the label's words (transactions.label_signature): groups
    # the operations that read alike without the label ever leaving its cipher.
    # NULL on rows stored before it existed, until transfer patterns backfill it.
    label_signature_bidx: str | None = Field(default=None, sa_column=Column(TEXT, index=True))
    # An OperationType (services/banking/operation_types.py). NULL on rows
    # stored before it existed, until transfer patterns backfill it.
    operation_type_enc: str | None = Field(default=None, sa_column=Column(TEXT))
    # A CashflowType the user forced on this one operation, over any rule.
    type_override_enc: str | None = Field(default=None, sa_column=Column(TEXT))

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


class BankTransferPatterns(SQLModel, table=True):
    """What the whole history of a user's movements says about transfers,
    derived and rebuilt from scratch, never updated in place.

    Pairing a month only loads that month and its neighbours; whether a pair's
    shape recurs is a question about every month at once. Reading the whole
    history on every request would cost a full decryption of every row, so the
    answer is kept here, with a digest of what it was built from: a reader
    finding the digest outdated rebuilds before reading, so no write path can
    leave it stale (see services/banking/transfer_patterns.py).
    """
    __tablename__ = "bank_transfer_patterns"
    __table_args__ = {"extend_existing": True}

    user_uuid_bidx: str = Field(sa_column=Column(TEXT, primary_key=True, nullable=False))
    source_bidx: str = Field(sa_column=Column(TEXT, nullable=False))
    # JSON: pair shapes and how often each occurred, the words too common on
    # each account side to tell a refund apart, and the open questions per month.
    content_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    built_at: datetime = Field(sa_column=Column(sa.DateTime(timezone=True), nullable=False))


class BankTransferDecision(SQLModel, table=True):
    """What the user settled about two stored movements: one transfer between
    their own accounts, not one, or a movement and its cancellation on a single
    account.

    The movements are referenced by a blind index of their uuid, never the uuid
    itself: `bank_transactions` carries no user column, and a clear reference
    here would tie its rows back to a user. Each leg keeps the tokens of its
    label at the time of the decision, so what the decision teaches outlives
    the rows themselves (see services/banking/transfer_decisions.py).
    """
    __tablename__ = "bank_transfer_decisions"
    __table_args__ = (
        UniqueConstraint(
            "user_uuid_bidx", "debit_ref_bidx", "credit_ref_bidx",
            name="uq_bank_transfer_decisions_pair",
        ),
        {"extend_existing": True},
    )

    uuid: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        sa_column=Column(TEXT, primary_key=True, nullable=False),
    )
    user_uuid_bidx: str = Field(sa_column=Column(TEXT, nullable=False, index=True))
    kind_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    debit_ref_bidx: str = Field(sa_column=Column(TEXT, nullable=False))
    credit_ref_bidx: str = Field(sa_column=Column(TEXT, nullable=False))
    # The same blind index as `BankTransaction.account_id_bidx`.
    debit_account_bidx: str = Field(sa_column=Column(TEXT, nullable=False))
    credit_account_bidx: str = Field(sa_column=Column(TEXT, nullable=False))
    debit_tokens_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    credit_tokens_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    # Set by the service, to the microsecond: decisions are replayed in order.
    created_at: datetime = Field(
        sa_column=Column(sa.DateTime(timezone=True), nullable=False)
    )


class BankTypeRule(SQLModel, table=True):
    """The cashflow type the user gave a label on one account and direction: it
    types every operation reading like it, past and future, as they are read
    (see services/banking/type_rules.py).

    Nothing here is joinable in clear with `bank_transactions`: the account, the
    direction and the label signature are only ever encrypted, and uniqueness
    rests on a blind index of the three together.
    """
    __tablename__ = "bank_type_rules"
    __table_args__ = (
        UniqueConstraint("user_uuid_bidx", "rule_bidx", name="uq_bank_type_rules_rule"),
        {"extend_existing": True},
    )

    uuid: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        sa_column=Column(TEXT, primary_key=True, nullable=False),
    )
    user_uuid_bidx: str = Field(sa_column=Column(TEXT, nullable=False, index=True))
    rule_bidx: str = Field(sa_column=Column(TEXT, nullable=False))
    signature_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    account_ref_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    credit_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    # JSON list of the label's words, for rules reaching nearby labels.
    words_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    type_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    # Set by the service, to the microsecond: the most recent rule wins a tie.
    created_at: datetime = Field(
        sa_column=Column(sa.DateTime(timezone=True), nullable=False)
    )
