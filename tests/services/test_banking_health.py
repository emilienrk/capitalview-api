"""
Tests for Enable Banking session health, consent lifecycle, and expiration checks (Task 7).
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlmodel import Session, select

from models.bank import BankAccount
from models.banking import BankAccountLink, BankSession
from models.notification import Notification, NotificationType
from models.user import User
from services.banking.health import (
    ALL_SESSION_STATUSES,
    STATUS_AUTHORIZED,
    STATUS_CANCELLED,
    STATUS_CLOSED,
    STATUS_EXPIRED,
    STATUS_INVALID,
    STATUS_PENDING_AUTHORIZATION,
    STATUS_RETURNED_FROM_BANK,
    STATUS_REVOKED,
    check_session_health,
    is_session_active,
    notify_user_expiring_consents,
    session_status_message,
)
from services.encryption import encrypt_data, hash_index


USER_UUID = "test-health-user-uuid"
MASTER_KEY = "test-master-key-32-chars-long!!"


@pytest.fixture(autouse=True)
def _user_row(session: Session) -> User:
    """`notifications.user_uuid` is a real foreign key to `users.uuid`."""
    user = User(
        uuid=USER_UUID,
        auth_salt="salt",
        username="health",
        email="health@test.com",
        password_hash="x",
    )
    session.add(user)
    session.commit()
    return user


def _seed_bank_session(
    session: Session,
    master_key: str,
    status: str = STATUS_AUTHORIZED,
    valid_until: datetime | None = None,
    user_uuid: str = USER_UUID,
    aspsp_name: str = "Boursorama",
) -> BankSession:
    user_bidx = hash_index(user_uuid, master_key)
    if valid_until is None:
        valid_until = datetime.now(timezone.utc) + timedelta(days=90)
    bank_session = BankSession(
        user_uuid_bidx=user_bidx,
        session_id_enc=encrypt_data("session-ext-id", master_key),
        aspsp_name_enc=encrypt_data(aspsp_name, master_key),
        aspsp_country_enc=encrypt_data("FR", master_key),
        status=status,
        consent_valid_until=valid_until,
        authorized_at=datetime.now(timezone.utc),
    )
    session.add(bank_session)
    session.commit()
    session.refresh(bank_session)
    return bank_session


def _seed_bank_link(
    session: Session,
    master_key: str,
    bank_session: BankSession,
    user_uuid: str = USER_UUID,
) -> tuple[BankAccount, BankAccountLink]:
    user_bidx = hash_index(user_uuid, master_key)
    account = BankAccount(
        user_uuid_bidx=user_bidx,
        name_enc=encrypt_data("Compte Test", master_key),
        balance_enc=encrypt_data("1000.00", master_key),
        account_type_enc=encrypt_data("CHECKING", master_key),
    )
    session.add(account)
    session.commit()
    session.refresh(account)

    link = BankAccountLink(
        user_uuid_bidx=user_bidx,
        bank_account_uuid_bidx=hash_index(account.uuid, master_key),
        session_uuid=bank_session.uuid,
        identification_hash_bidx=hash_index("ih-durable-key", master_key),
        account_uid_enc=encrypt_data("uid-disposable", master_key),
        anchor_date=date.today(),
        anchor_balance_enc=encrypt_data("1000.00", master_key),
        last_synced_at=date.today(),
    )
    session.add(link)
    session.commit()
    session.refresh(link)
    return account, link


class TestSessionStatuses:
    def test_eight_statuses_defined_and_distinct(self):
        # The constants must spell the SessionStatus enum from
        # vendor-docs/enablebanking-api.yaml exactly — the spec's descriptions
        # are shuffled, so a wrong spelling here would never surface at runtime.
        assert ALL_SESSION_STATUSES == {
            STATUS_AUTHORIZED,
            STATUS_EXPIRED,
            STATUS_REVOKED,
            STATUS_CLOSED,
            STATUS_CANCELLED,
            STATUS_INVALID,
            STATUS_PENDING_AUTHORIZATION,
            STATUS_RETURNED_FROM_BANK,
        }
        assert ALL_SESSION_STATUSES == {
            "AUTHORIZED",
            "EXPIRED",
            "REVOKED",
            "CLOSED",
            "CANCELLED",
            "INVALID",
            "PENDING_AUTHORIZATION",
            "RETURNED_FROM_BANK",
        }

    def test_is_session_active(self):
        assert is_session_active(STATUS_AUTHORIZED) is True
        for st in ALL_SESSION_STATUSES - {STATUS_AUTHORIZED}:
            assert is_session_active(st) is False

    def test_session_status_messages_distinguished(self):
        messages = {st: session_status_message(st) for st in ALL_SESSION_STATUSES}
        # All messages should be unique and informative
        assert len(set(messages.values())) == 8
        assert "expiré" in messages[STATUS_EXPIRED].lower()
        assert "révoqué" in messages[STATUS_REVOKED].lower()
        assert "clôturée" in messages[STATUS_CLOSED].lower()


class TestCheckSessionHealth:
    def test_expired_session_transitions_to_expired_without_master_key(self, session: Session, master_key: str):
        past = datetime.now(timezone.utc) - timedelta(hours=2)
        bank_session = _seed_bank_session(session, master_key, status=STATUS_AUTHORIZED, valid_until=past)

        # Runs with no master key
        updated_count = check_session_health(session)
        assert updated_count == 1

        session.refresh(bank_session)
        assert bank_session.status == STATUS_EXPIRED

    def test_session_expiration_preserves_bank_account_link(self, session: Session, master_key: str):
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        bank_session = _seed_bank_session(session, master_key, status=STATUS_AUTHORIZED, valid_until=past)
        account, link = _seed_bank_link(session, master_key, bank_session)

        check_session_health(session)

        session.refresh(bank_session)
        assert bank_session.status == STATUS_EXPIRED

        # Link is completely preserved
        refreshed_link = session.get(BankAccountLink, link.uuid)
        assert refreshed_link is not None
        assert refreshed_link.session_uuid == bank_session.uuid
        assert refreshed_link.identification_hash_bidx == link.identification_hash_bidx

    def test_active_session_is_not_modified(self, session: Session, master_key: str):
        future = datetime.now(timezone.utc) + timedelta(days=30)
        bank_session = _seed_bank_session(session, master_key, status=STATUS_AUTHORIZED, valid_until=future)

        updated_count = check_session_health(session)
        assert updated_count == 0

        session.refresh(bank_session)
        assert bank_session.status == STATUS_AUTHORIZED


class TestExpiringConsentNotifications:
    def test_notify_expiring_consent_creates_notification(self, session: Session, master_key: str):
        expiring_soon = datetime.now(timezone.utc) + timedelta(days=4)
        _seed_bank_session(session, master_key, status=STATUS_AUTHORIZED, valid_until=expiring_soon)

        count = notify_user_expiring_consents(session, USER_UUID, master_key, within_days=7)
        assert count == 1

        notifs = session.exec(select(Notification).where(Notification.user_uuid == USER_UUID)).all()
        assert len(notifs) == 1
        assert notifs[0].type == NotificationType.BANK_CONSENT_EXPIRING
        assert "Boursorama" in notifs[0].message
        assert "expire" in notifs[0].message

    def test_notify_expiring_consent_avoids_duplicate_unread(self, session: Session, master_key: str):
        expiring_soon = datetime.now(timezone.utc) + timedelta(days=4)
        _seed_bank_session(session, master_key, status=STATUS_AUTHORIZED, valid_until=expiring_soon)

        assert notify_user_expiring_consents(session, USER_UUID, master_key, within_days=7) == 1
        # Second call does not create a duplicate unread notification
        assert notify_user_expiring_consents(session, USER_UUID, master_key, within_days=7) == 0

        notifs = session.exec(select(Notification).where(Notification.user_uuid == USER_UUID)).all()
        assert len(notifs) == 1

    def test_the_keyless_job_marks_expiry_but_never_notifies(
        self, session: Session, master_key: str
    ):
        """Ruling R20, written down as a test.

        The nightly job has no Master Key, so it cannot recover the clear-text
        `user_uuid` a Notification is keyed by from a session that carries only
        `user_uuid_bidx`. It marks; the sync path warns.
        """
        past = datetime.now(timezone.utc) - timedelta(hours=2)
        soon = datetime.now(timezone.utc) + timedelta(days=3)
        _seed_bank_session(session, master_key, valid_until=past)
        _seed_bank_session(session, master_key, valid_until=soon, aspsp_name="Autre Banque")

        assert check_session_health(session) == 1

        assert (
            session.exec(select(Notification).where(Notification.user_uuid == USER_UUID)).all()
            == []
        )
