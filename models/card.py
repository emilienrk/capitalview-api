"""
Card model for AI insights.
"""
import uuid
from datetime import datetime
from sqlmodel import SQLModel, Field
import sqlalchemy as sa
from sqlalchemy import Column, TEXT


class Card(SQLModel, table=True):
    """User generated AI cards."""
    __tablename__ = "cards"
    __table_args__ = {"extend_existing": True}

    uuid: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    user_uuid_bidx: str = Field(sa_column=Column(TEXT, nullable=False, index=True))
    title_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    text_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    theme_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    scope_enc: str = Field(sa_column=Column(TEXT, nullable=False))

    created_at: datetime = Field(
        default=sa.func.now(),
        sa_column=Column(sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)
    )