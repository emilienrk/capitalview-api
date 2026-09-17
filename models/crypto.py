"""
CryptoAccount and CryptoTransaction models.
"""
from datetime import datetime, date
from sqlmodel import SQLModel, Field
import sqlalchemy as sa
from sqlalchemy import Column, TEXT
import uuid


class CryptoAccount(SQLModel, table=True):
    """Crypto wallets and exchanges."""
    __tablename__ = "crypto_accounts"
    __table_args__ = {"extend_existing": True}

    uuid: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    user_uuid_bidx: str = Field(sa_column=Column(TEXT, nullable=False, index=True))
    name_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    platform_enc: str | None = Field(sa_column=Column(TEXT))
    public_address_enc: str | None = Field(sa_column=Column(TEXT))
    
    # Date the crypto account was actually opened (user-supplied)
    opened_at: date | None = Field(
        default=None,
        sa_column=Column(sa.Date, nullable=True),
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


class CryptoTransaction(SQLModel, table=True):
    """History of buy/sell for crypto."""
    __tablename__ = "crypto_transactions"
    __table_args__ = {"extend_existing": True}

    uuid: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    account_id_bidx: str = Field(sa_column=Column(TEXT, nullable=False, index=True))
    group_uuid: str | None = Field(default=None, sa_column=Column(TEXT, index=True))
    asset_key_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    type_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    amount_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    price_per_unit_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    executed_at_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    tx_hash_enc: str | None = Field(sa_column=Column(TEXT))
    notes_enc: str | None = Field(sa_column=Column(TEXT))
    # The EUR leg the app writes itself beside another row of the same group: a
    # purchase's funding, a sale's proceeds. Same meaning as on a stock
    # transaction, and in clear for the same reasons — no money crossed the
    # account's boundary, so nothing may read it as a transfer from the bank.
    # False on rows stored before it existed, until the deposits are read once
    # (services/banking/contributions.py).
    is_auto_provision: bool = Field(
        default=False,
        sa_column=Column(sa.Boolean, nullable=False, server_default=sa.false()),
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