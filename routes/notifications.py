"""Notification routes: list community events and mark them read."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlmodel import Session

from database import get_session
from dtos.notification import NotificationListResponse, NotificationResponse
from models.user import User
from services.auth import get_current_user
from services.notification import count_unread, list_notifications, mark_all_read

router = APIRouter(prefix="/notifications", tags=["Notifications"])


@router.get("", response_model=NotificationListResponse)
def get_notifications(
    current_user: Annotated[User, Depends(get_current_user)],
    session: Session = Depends(get_session),
):
    """List the user's notifications, most recent first, with the unread count.

    The count is returned alongside the list so the UI needs a single call to
    decide whether to render the indicator at all.
    """
    return NotificationListResponse(
        unread_count=count_unread(session, current_user.uuid),
        notifications=[
            NotificationResponse.model_validate(n)
            for n in list_notifications(session, current_user.uuid)
        ],
    )


@router.post("/read", response_model=NotificationListResponse)
def mark_notifications_read(
    current_user: Annotated[User, Depends(get_current_user)],
    session: Session = Depends(get_session),
):
    """Mark every notification as read and return the refreshed list."""
    mark_all_read(session, current_user.uuid)
    return NotificationListResponse(
        unread_count=0,
        notifications=[
            NotificationResponse.model_validate(n)
            for n in list_notifications(session, current_user.uuid)
        ],
    )
