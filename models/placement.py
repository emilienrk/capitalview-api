"""
PlacementAccount and PlacementEntry models.

A placement followed by hand: no provider exposes an API, so its value comes
from the balances the user reads on a statement, and its deposits and
withdrawals are recorded so the bank side can recognise the transfers.
"""
from datetime import date, datetime
from sqlmodel import SQLModel, Field
import sqlalchemy as sa
from sqlalchemy import Column, TEXT
import uuid


class PlacementAccount(SQLModel, table=True):
    """An AV, a PER, a SCPI, employee savings… anything valued from statements."""
    __tablename__ = "placement_accounts"
    __table_args__ = {"extend_existing": True}

    uuid: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    user_uuid_bidx: str = Field(sa_column=Column(TEXT, nullable=False, index=True))
    name_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    institution_name_enc: str | None = Field(sa_column=Column(TEXT))
    placement_type_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    # The user's own assumption, only ever used to project — never to value.
    expected_return_rate_enc: str | None = Field(default=None, sa_column=Column(TEXT))

    # Starts the eight-year clock of an AV's tax treatment (user-supplied)
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


class PlacementEntry(SQLModel, table=True):
    """A statement balance, a deposit or a withdrawal on a placement."""
    __tablename__ = "placement_entries"
    __table_args__ = {"extend_existing": True}

    uuid: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    account_uuid: str = Field(
        sa_column=Column(
            TEXT,
            sa.ForeignKey("placement_accounts.uuid", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    type_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    amount_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    occurred_at_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    note_enc: str | None = Field(default=None, sa_column=Column(TEXT))

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
