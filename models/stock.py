"""   
StockAccount and StockTransaction models.
"""
from datetime import datetime, date
from sqlmodel import SQLModel, Field
import sqlalchemy as sa
from sqlalchemy import Column, TEXT
import uuid


class StockAccount(SQLModel, table=True):
    """Investment accounts (PEA, CTO)."""
    __tablename__ = "stock_accounts"
    __table_args__ = {"extend_existing": True}

    uuid: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    user_uuid_bidx: str = Field(sa_column=Column(TEXT, nullable=False, index=True))
    name_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    institution_name_enc: str | None = Field(sa_column=Column(TEXT))
    identifier_enc: str | None = Field(sa_column=Column(TEXT))
    account_type_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    
    # Date the stock account was actually opened (user-supplied)
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

class StockTransaction(SQLModel, table=True):
    """History of buy/sell for stocks."""
    __tablename__ = "stock_transactions"
    __table_args__ = {"extend_existing": True}

    uuid: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    account_id_bidx: str = Field(sa_column=Column(TEXT, nullable=False, index=True))
    asset_key_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    type_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    amount_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    price_per_unit_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    fees_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    executed_at_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    notes_enc: str | None = Field(sa_column=Column(TEXT))
    # A EUR deposit the app wrote itself to cover a BUY the cash was short for
    # (services/stock_transaction.py). It is bookkeeping: no money crossed the
    # account's boundary, so nothing may read it as a transfer from the bank.
    # In clear, unlike everything else here: it says nothing the row's existence
    # does not already say, and the bank reading needs it without the key.
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