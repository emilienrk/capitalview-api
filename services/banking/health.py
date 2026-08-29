"""
Consent lifecycle and health check for Enable Banking sessions.

The 8 SessionStatus members are matched by NAME (the OpenAPI spec's enum
descriptions are misaligned with their values, documented trap).

Operational metadata (`status` and `consent_valid_until`) are stored in clear text
on `BankSession` so that scheduled background checks can update expired sessions
without requiring a Master Key.

**Two halves, and only one of them is a background job (ruling R20).**
`check_session_health` runs keyless in the nightly scheduler: it reads clear-text
columns and writes clear-text columns. `notify_user_expiring_consents` cannot,
because a `Notification` row is keyed by a clear-text `user_uuid` and a
`BankSession` carries only `user_uuid_bidx` — a keyless job cannot recover the
one from the other. Adding the clear-text `user_uuid` to `bank_sessions` would
recover it, and was rejected: it would make the database itself reveal which
users hold a bank connection.

**Accepted limitation, deliberately traded:** the warning is therefore produced
from the sync path, where a Master Key exists — so the user is warned when they
next open CapitalView, not while they are away. On a ninety-day consent with a
seven-day window that is a wide enough net, and confidentiality wins over
reaching a user who is not looking.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import sqlalchemy as sa
from sqlmodel import Session, select

from models.banking import BankAccountLink, BankSession
from models.notification import Notification, NotificationType
from services.encryption import decrypt_data, hash_index

logger = logging.getLogger(__name__)

# The 8 SessionStatus members, spelled exactly as the OpenAPI `enum` list — the
# spec's x-enum-descriptions are shuffled against their values, so only the enum
# entries themselves can be trusted, never a value's position or its description.
STATUS_AUTHORIZED = "AUTHORIZED"
STATUS_EXPIRED = "EXPIRED"
STATUS_REVOKED = "REVOKED"
STATUS_CLOSED = "CLOSED"
STATUS_CANCELLED = "CANCELLED"
STATUS_INVALID = "INVALID"
STATUS_PENDING_AUTHORIZATION = "PENDING_AUTHORIZATION"
STATUS_RETURNED_FROM_BANK = "RETURNED_FROM_BANK"

ALL_SESSION_STATUSES = frozenset({
    STATUS_AUTHORIZED,
    STATUS_EXPIRED,
    STATUS_REVOKED,
    STATUS_CLOSED,
    STATUS_CANCELLED,
    STATUS_INVALID,
    STATUS_PENDING_AUTHORIZATION,
    STATUS_RETURNED_FROM_BANK,
})

# Status explanations mapped to distinct messages
SESSION_STATUS_MESSAGES: dict[str, str] = {
    STATUS_AUTHORIZED: "Consentement actif et autorisé.",
    STATUS_EXPIRED: "Consentement expiré. Reconnectez votre banque pour reprendre la synchronisation.",
    STATUS_REVOKED: "Consentement révoqué auprès de votre banque. Une nouvelle autorisation est nécessaire.",
    STATUS_CLOSED: "Connexion clôturée.",
    STATUS_CANCELLED: "Autorisation annulée par l'utilisateur.",
    STATUS_INVALID: "Session bancaire invalide ou inconnue.",
    STATUS_PENDING_AUTHORIZATION: "En attente de l'autorisation bancaire.",
    STATUS_RETURNED_FROM_BANK: "Retour de la banque reçu, autorisation en cours de finalisation.",
}

# How many days in advance of consent_valid_until to notify the user
EXPIRATION_NOTIFICATION_WINDOW_DAYS = 7


def is_session_active(status: str | None) -> bool:
    """Whether a session is currently authorized and usable.

    `None` (a link whose session row is gone) is not active.
    """
    return status == STATUS_AUTHORIZED


def session_status_message(status: str) -> str:
    """Return the user-facing explanation for a given session status."""
    return SESSION_STATUS_MESSAGES.get(status, "Statut de session bancaire inconnu.")


def check_session_health(session: Session, now: datetime | None = None) -> int:
    """Inspect all bank sessions and transition expired ones to status=EXPIRED.

    Runs without needing any Master Key since `status` and `consent_valid_until`
    are in cleartext.
    
    CRITICAL: Preserves all `BankAccountLink` rows! Session expiration changes
    the consent status but NEVER destroys account attachments.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    # Find authorized sessions that have passed their valid_until cutoff
    expired_sessions = session.exec(
        select(BankSession).where(
            BankSession.status == STATUS_AUTHORIZED,
            BankSession.consent_valid_until <= now,
        )
    ).all()

    if not expired_sessions:
        return 0

    for s in expired_sessions:
        s.status = STATUS_EXPIRED
        session.add(s)

    session.commit()
    logger.info("check_session_health: marked %d bank sessions as EXPIRED", len(expired_sessions))
    return len(expired_sessions)


def notify_user_expiring_consents(
    session: Session,
    user_uuid: str,
    master_key: str,
    within_days: int = EXPIRATION_NOTIFICATION_WINDOW_DAYS,
    now: datetime | None = None,
) -> int:
    """Check if the user has any active bank consent expiring soon and send a notification.

    Called from `sync_user_accounts`, never from the scheduler: it needs the
    Master Key twice over — to reach this user's sessions through their blind
    index, and to write the clear-text `user_uuid` a `Notification` is keyed by
    (see the module docstring for the trade this settles).

    Prevents duplicate notifications for the same session expiry window.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    user_bidx = hash_index(user_uuid, master_key)
    cutoff = now + timedelta(days=within_days)

    active_sessions = session.exec(
        select(BankSession).where(
            BankSession.user_uuid_bidx == user_bidx,
            BankSession.status == STATUS_AUTHORIZED,
            BankSession.consent_valid_until > now,
            BankSession.consent_valid_until <= cutoff,
        )
    ).all()

    if not active_sessions:
        return 0

    # Check recent unread bank_consent_expiring notifications to avoid spamming
    recent_notifs = session.exec(
        select(Notification).where(
            Notification.user_uuid == user_uuid,
            Notification.type == NotificationType.BANK_CONSENT_EXPIRING,
            Notification.read_at == None,  # noqa: E711
        )
    ).all()

    notified = 0
    for s in active_sessions:
        aspsp_name = None
        if s.aspsp_name_enc:
            try:
                aspsp_name = decrypt_data(s.aspsp_name_enc, master_key)
            except Exception:
                aspsp_name = None

        bank_label = aspsp_name or "votre banque"
        expiry_str = s.consent_valid_until.strftime("%d/%m/%Y")
        msg = (
            f"Votre connexion bancaire auprès de {bank_label} expire le {expiry_str}. "
            "Reconnectez votre compte pour maintenir la synchronisation automatique."
        )

        # Skip if an unread notification with identical bank already exists
        if any(bank_label in n.message for n in recent_notifs):
            continue

        notification = Notification(
            user_uuid=user_uuid,
            type=NotificationType.BANK_CONSENT_EXPIRING,
            message=msg,
        )
        session.add(notification)
        notified += 1

    if notified > 0:
        session.commit()

    return notified


def check_all_consents_daily() -> None:
    """Daily cron task: mark consents that have passed their cutoff as EXPIRED.

    It does **not** notify. Notifying needs a Master Key this job does not have
    (ruling R20); `sync_user_accounts` carries that half.
    """
    from database import get_engine

    engine = get_engine()
    with Session(engine) as session:
        check_session_health(session)
