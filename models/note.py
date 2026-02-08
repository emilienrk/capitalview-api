"""
Note model (user notes).
"""
from typing import Optional
from datetime import datetime
from sqlmodel import SQLModel, Field
import sqlalchemy as sa
from sqlalchemy import Column, TEXT

class Note(SQLModel, table=True):
    """User notes."""
    __tablename__ = "notes"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_uuid_bidx: str = Field(sa_column=Column(TEXT, nullable=False))
    name_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    description_enc: str = Field(sa_column=Column(TEXT, nullable=False))

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