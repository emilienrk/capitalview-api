"""   
StockAccount and StockTransaction models.
"""
from typing import Optional
from datetime import datetime
from sqlmodel import SQLModel, Field
import sqlalchemy as sa
from sqlalchemy import Column, TEXT
import uuid


class StockAccount(SQLModel, table=True):
    """Investment accounts (PEA, CTO)."""
    __tablename__ = "stock_accounts"
    __table_args__ = {"extend_existing": True}

    uuid: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    user_uuid_bidx: str = Field(sa_column=Column(TEXT, nullable=False))
    name_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    institution_name_enc: Optional[str] = Field(sa_column=Column(TEXT))
    identifier_enc: Optional[str] = Field(sa_column=Column(TEXT))
    account_type_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    
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

class StockTransaction(SQLModel, table=True):
    """History of buy/sell for stocks."""
    __tablename__ = "stock_transactions"
    __table_args__ = {"extend_existing": True}

    uuid: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    account_id_bidx: str = Field(sa_column=Column(TEXT, nullable=False))
    symbol_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    isin_enc: Optional[str] = Field(sa_column=Column(TEXT))
    exchange_enc: Optional[str] = Field(sa_column=Column(TEXT))
    type_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    amount_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    price_per_unit_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    fees_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    executed_at_enc: str = Field(sa_column=Column(TEXT, nullable=False))
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