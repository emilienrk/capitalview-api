"""Notification DTOs."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class NotificationResponse(BaseModel):
    """A single notification shown in the panel."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    type: str
    message: str
    actor_username: str | None = None
    asset_key: str | None = None
    read_at: datetime | None = None
    created_at: datetime


class NotificationListResponse(BaseModel):
    """The panel's whole payload: the list plus the badge count."""
    unread_count: int
    notifications: list[NotificationResponse] = []
