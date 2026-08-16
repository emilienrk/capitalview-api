"""
Notification service — create, read and mark community events.

Notifications are best-effort: failing to record one must never break the
action that triggered it (following someone, running the nightly price job).
"""

import logging
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlmodel import Session, select

from models.community import CommunityPick
from models.notification import Notification, NotificationType
from models.user import User

logger = logging.getLogger(__name__)

# What the panel shows at most. Older rows stay in the table for counting but
# are never rendered — nobody scrolls a notification list past this.
MAX_NOTIFICATIONS = 50


def create_notification(
    session: Session,
    user_uuid: str,
    notification_type: str,
    message: str,
    actor_username: str | None = None,
    asset_key: str | None = None,
    commit: bool = True,
) -> Notification | None:
    """Record a notification. Returns None if it could not be stored.

    Swallows its own errors: a notification is a nicety, and losing one is
    always preferable to failing the action that produced it.
    """
    try:
        notification = Notification(
            user_uuid=user_uuid,
            type=notification_type,
            message=message,
            actor_username=actor_username,
            asset_key=asset_key,
        )
        session.add(notification)
        if commit:
            session.commit()
        return notification
    except Exception:
        logger.exception("Could not store %s notification for %s", notification_type, user_uuid)
        session.rollback()
        return None


def notify_new_follower(
    session: Session, followed_uuid: str, follower_username: str, is_mutual: bool
) -> None:
    """Tell someone they gained a follower.

    A mutual follow gets its own wording: it is the moment a private profile
    actually becomes visible to the other person, which is worth knowing.
    """
    if is_mutual:
        create_notification(
            session,
            followed_uuid,
            NotificationType.MUTUAL_FOLLOW,
            f"Vous vous suivez désormais mutuellement avec {follower_username}. "
            "Vos positions partagées sont visibles entre vous.",
            actor_username=follower_username,
        )
    else:
        create_notification(
            session,
            followed_uuid,
            NotificationType.NEW_FOLLOWER,
            f"{follower_username} vous suit désormais.",
            actor_username=follower_username,
        )


def list_notifications(session: Session, user_uuid: str) -> list[Notification]:
    """Most recent notifications first."""
    return list(session.exec(
        select(Notification)
        .where(Notification.user_uuid == user_uuid)
        .order_by(Notification.created_at.desc())
        .limit(MAX_NOTIFICATIONS)
    ).all())


def count_unread(session: Session, user_uuid: str) -> int:
    """How many notifications the user has not seen yet."""
    return len(session.exec(
        select(Notification).where(
            Notification.user_uuid == user_uuid,
            Notification.read_at == None,  # noqa: E711
        )
    ).all())


def mark_all_read(session: Session, user_uuid: str) -> int:
    """Mark every unread notification as read. Returns how many were updated."""
    result = session.exec(
        sa.update(Notification)
        .where(
            Notification.user_uuid == user_uuid,
            Notification.read_at == None,  # noqa: E711
        )
        .values(read_at=datetime.now(timezone.utc))
    )
    session.commit()
    return result.rowcount or 0


def check_pick_targets(session: Session) -> int:
    """Notify every user whose pick has reached its target price.

    Run after the nightly price update. Only fires once per pick: a pick whose
    target notification already exists is skipped, otherwise an asset sitting
    above its target would notify every single night.
    """
    from services.community import _asset_price

    picks = session.exec(
        select(CommunityPick).where(CommunityPick.target_price != None)  # noqa: E711
    ).all()

    already_notified = {
        (n.user_uuid, n.asset_key)
        for n in session.exec(
            select(Notification).where(
                Notification.type == NotificationType.PICK_TARGET_REACHED
            )
        ).all()
    }

    sent = 0
    for pick in picks:
        if (pick.user_id, pick.asset_key) in already_notified:
            continue
        try:
            current = _asset_price(session, pick.asset_key, pick.asset_type)
        except Exception:
            logger.exception("Price lookup failed for pick %s", pick.id)
            continue
        if not current or float(current) < pick.target_price:
            continue

        user = session.get(User, pick.user_id)
        if not user:
            continue

        create_notification(
            session,
            pick.user_id,
            NotificationType.PICK_TARGET_REACHED,
            f"{pick.asset_key} a atteint votre objectif de "
            f"{pick.target_price:g} € (cours actuel {float(current):.2f} €).",
            asset_key=pick.asset_key,
        )
        sent += 1

    return sent
