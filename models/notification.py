"""
Notification model.

Community events worth telling a user about: someone followed them, one of
their picks reached its target. Stored in cleartext — unlike the rest of the
app, these rows are produced by a background job that has no access to any
Master Key, and they only ever contain data already public in the community
module (usernames, asset keys).
"""
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import Column, TEXT
from sqlmodel import Field, SQLModel


class NotificationType:
    """Kinds of notification. Plain strings so adding one needs no migration."""
    NEW_FOLLOWER = "new_follower"
    MUTUAL_FOLLOW = "mutual_follow"
    PICK_TARGET_REACHED = "pick_target_reached"
    BANK_CONSENT_EXPIRING = "bank_consent_expiring"


class Notification(SQLModel, table=True):
    """A single event shown in the user's notification panel."""

    __tablename__ = "notifications"
    __table_args__ = {"extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    user_uuid: str = Field(
        sa_column=Column(
            sa.String,
            sa.ForeignKey("users.uuid", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    type: str = Field(sa_column=Column(sa.String(40), nullable=False))
    # Who or what the notification is about, for deep-linking from the panel.
    actor_username: str | None = Field(default=None, sa_column=Column(sa.String(50), nullable=True))
    asset_key: str | None = Field(default=None, sa_column=Column(sa.String(30), nullable=True))
    message: str = Field(sa_column=Column(TEXT, nullable=False))
    read_at: datetime | None = Field(
        default=None, sa_column=Column(sa.DateTime(timezone=True), nullable=True)
    )
    created_at: datetime = Field(
        default=sa.func.now(),
        sa_column=Column(
            sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False, index=True
        ),
    )
