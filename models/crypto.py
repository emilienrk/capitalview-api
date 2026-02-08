"""
CryptoAccount and CryptoTransaction models.
"""
from typing import Optional
from datetime import datetime
from sqlmodel import SQLModel, Field
import sqlalchemy as sa
from sqlalchemy import Column, TEXT


class CryptoAccount(SQLModel, table=True):
    """Crypto wallets and exchanges."""
    __tablename__ = "crypto_accounts"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_uuid_bidx: str = Field(sa_column=Column(TEXT, nullable=False))
    name_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    platform_enc: str = Field(sa_column=Column(TEXT, nullable=False)) # Renamed from wallet_name
    public_address_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    
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

    id: Optional[int] = Field(default=None, primary_key=True)
    account_id_bidx: str = Field(sa_column=Column(TEXT, nullable=False))
    ticker_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    type_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    amount_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    price_per_unit_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    fees_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    fees_ticker_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    executed_at_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    tx_hash_enc: Optional[str] = Field(sa_column=Column(TEXT))
    notes_enc: Optional[str] = Field(sa_column=Column(TEXT))

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