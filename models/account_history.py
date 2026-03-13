"""
AccountHistory model.
Stores daily snapshots of account valuations for performance charting.
Encrypted fields follow the project's E2EE convention (AES-256-GCM, Base64).
Blind indexes allow server-side filtering without exposing plaintext identifiers.
"""

from datetime import date, datetime
from typing import Optional

import sqlalchemy as sa
from sqlalchemy import Column, TEXT, UniqueConstraint
from sqlmodel import Field, SQLModel

import uuid

from models.enums import AccountCategory


class AccountHistory(SQLModel, table=True):
    """Daily valuation snapshot for an investment account."""

    __tablename__ = "account_history"
    __table_args__ = (
        UniqueConstraint("account_id_bidx", "snapshot_date", name="uq_account_history_account_date"),
        {"extend_existing": True},
    )

    uuid: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        primary_key=True,
    )
    user_uuid_bidx: str = Field(sa_column=Column(TEXT, nullable=False, index=True))
    account_id_bidx: str = Field(sa_column=Column(TEXT, nullable=False, index=True))
    account_type: AccountCategory = Field(sa_column=Column(TEXT, nullable=False, index=True))
    snapshot_date: date = Field(sa_column=Column(sa.Date, nullable=False))
    total_value_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    total_invested_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    daily_pnl_enc: Optional[str] = Field(default=None, sa_column=Column(TEXT))
    positions_enc: Optional[str] = Field(default=None, sa_column=Column(TEXT))
    created_at: datetime = Field(
        default=sa.func.now(),
        sa_column=Column(
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    updated_at: datetime = Field(
        default=sa.func.now(),
        sa_column=Column(
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
    )
