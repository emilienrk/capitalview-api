"""Notifications raised by community events."""

import os
import base64

# Must be set before any project module calls get_settings() for the first time
_TEST_COMMUNITY_KEY = base64.b64encode(b"C" * 32).decode()
os.environ.setdefault("COMMUNITY_ENCRYPTION_KEY", _TEST_COMMUNITY_KEY)

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from main import app
from models.community import CommunityFollow, CommunityPick, CommunityProfile
from models.notification import Notification, NotificationType
from models.user import User


def _make_user(uuid: str, username: str) -> User:
    return User(
        uuid=uuid, auth_salt="salt", username=username,
        email=f"{username}@test.com", password_hash="x",
    )


@pytest.fixture()
def alice(session) -> User:
    u = _make_user("user_1", "alice")
    session.add(u)
    session.flush()
    return u


@pytest.fixture()
def bob(session) -> User:
    u = _make_user("user_2", "bob")
    session.add(u)
    session.add(CommunityProfile(user_id="user_2", is_active=True, is_private=False))
    session.flush()
    return u


@pytest.fixture(autouse=True)
def _override_deps(session, master_key, alice):
    from config import get_settings
    get_settings.cache_clear()
    os.environ["COMMUNITY_ENCRYPTION_KEY"] = _TEST_COMMUNITY_KEY

    app.dependency_overrides.clear()
    from database import get_session as _db_get_session
    from services.auth import get_current_user, get_master_key as _get_mk

    app.dependency_overrides[_db_get_session] = lambda: session
    app.dependency_overrides[get_current_user] = lambda: alice
    app.dependency_overrides[_get_mk] = lambda: master_key

    yield

    app.dependency_overrides.clear()
    get_settings.cache_clear()


def test_following_someone_notifies_them(session, bob):
    client = TestClient(app)

    assert client.post("/community/follow/bob").status_code == 200

    notifications = session.exec(
        select(Notification).where(Notification.user_uuid == "user_2")
    ).all()
    assert len(notifications) == 1
    assert notifications[0].type == NotificationType.NEW_FOLLOWER
    assert notifications[0].actor_username == "alice"


def test_a_mutual_follow_says_so(session, bob):
    """The moment a private profile actually opens up deserves its own wording."""
    client = TestClient(app)
    session.add(CommunityFollow(follower_id="user_2", following_id="user_1"))
    session.flush()

    assert client.post("/community/follow/bob").status_code == 200

    notification = session.exec(
        select(Notification).where(Notification.user_uuid == "user_2")
    ).one()
    assert notification.type == NotificationType.MUTUAL_FOLLOW


def test_listing_and_marking_read(session, alice):
    client = TestClient(app)
    session.add(Notification(
        user_uuid="user_1", type=NotificationType.NEW_FOLLOWER, message="bob vous suit."
    ))
    session.flush()

    listed = client.get("/notifications")
    assert listed.status_code == 200
    assert listed.json()["unread_count"] == 1

    read = client.post("/notifications/read")
    assert read.status_code == 200
    assert read.json()["unread_count"] == 0
    # The notification stays in the list — marking read is not deleting.
    assert len(read.json()["notifications"]) == 1


def test_a_user_only_sees_their_own_notifications(session, alice, bob):
    client = TestClient(app)
    session.add(Notification(
        user_uuid="user_2", type=NotificationType.NEW_FOLLOWER, message="pas pour alice"
    ))
    session.flush()

    body = client.get("/notifications").json()
    assert body["unread_count"] == 0
    assert body["notifications"] == []


def test_a_reached_target_notifies_once(session, alice, monkeypatch):
    """Fires on the first run only — otherwise it would notify every night."""
    from decimal import Decimal
    import services.community as community_service
    from services.notification import check_pick_targets

    session.add(CommunityPick(
        user_id="user_1", asset_key="US0378331005", asset_type="STOCK", target_price=100.0
    ))
    session.flush()

    monkeypatch.setattr(
        community_service, "_asset_price", lambda *a, **k: Decimal("150")
    )

    assert check_pick_targets(session) == 1
    assert check_pick_targets(session) == 0

    notification = session.exec(
        select(Notification).where(
            Notification.type == NotificationType.PICK_TARGET_REACHED
        )
    ).one()
    assert notification.asset_key == "US0378331005"


def test_a_target_still_out_of_reach_stays_quiet(session, alice, monkeypatch):
    from decimal import Decimal
    import services.community as community_service
    from services.notification import check_pick_targets

    session.add(CommunityPick(
        user_id="user_1", asset_key="US0378331005", asset_type="STOCK", target_price=500.0
    ))
    session.flush()

    monkeypatch.setattr(
        community_service, "_asset_price", lambda *a, **k: Decimal("150")
    )

    assert check_pick_targets(session) == 0
