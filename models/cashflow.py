"""Cashflow model (income and expenses)."""

from datetime import datetime
from typing import Optional
from sqlmodel import SQLModel, Field
import sqlalchemy as sa
from sqlalchemy import Column, TEXT
import uuid


class Cashflow(SQLModel, table=True):
    """Merged Income and Expenses table."""
    __tablename__ = "cashflows"
    __table_args__ = {"extend_existing": True}

    uuid: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    user_uuid_bidx: str = Field(sa_column=Column(TEXT, nullable=False, index=True))
    name_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    flow_type_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    category_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    amount_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    frequency_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    transaction_date_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    # Blind index to link to a bank account (queryable without decryption)
    bank_account_uuid_bidx: Optional[str] = Field(default=None, sa_column=Column(TEXT, nullable=True, index=True))
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