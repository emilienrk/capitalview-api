"""
UserBankConnection model.

BYO Enable Banking credentials: each user brings their own application_id and
private key, since Enable Banking's free tier only exposes accounts linked by
the account holder themselves (see services/banking/credentials.py).
"""
from datetime import datetime
from sqlmodel import SQLModel, Field
from sqlalchemy import Column, TEXT
import sqlalchemy as sa


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
