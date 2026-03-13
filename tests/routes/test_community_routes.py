"""Tests for community routes.

Endpoints covered:
  - GET/PUT /community/settings
  - GET/POST/PUT/DELETE /community/picks/*
  - POST/DELETE /community/follow/{username}
  - GET /community/profiles
  - GET /community/profiles/{username}
  - GET /community/search

Setup rules:
  - current user is "alice" (user_1) — inserted in DB per test
  - secondary user is "bob" (user_2) — inserted per test when needed
  - COMMUNITY_ENCRYPTION_KEY is provided so encrypt/decrypt works
"""

import os
import base64

# Must be set before any project module calls get_settings() for the first time
_TEST_COMMUNITY_KEY = base64.b64encode(b"C" * 32).decode()
os.environ.setdefault("COMMUNITY_ENCRYPTION_KEY", _TEST_COMMUNITY_KEY)

import pytest
from fastapi.testclient import TestClient

from main import app
from models.user import User
from models.community import CommunityProfile, CommunityPosition, CommunityFollow


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_user(uuid: str, username: str, email: str) -> User:
    return User(uuid=uuid, auth_salt="salt", username=username, email=email, password_hash="x")


def _activate_profile(session, user_uuid: str, *, is_private: bool = False, display_name: str | None = None) -> CommunityProfile:
    """Insert an active CommunityProfile for the given user."""
    profile = CommunityProfile(
        user_id=user_uuid,
        is_active=True,
        is_private=is_private,
        display_name=display_name,
    )
    session.add(profile)
    session.flush()
    return profile


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture()
def alice(session) -> User:
    u = _make_user("user_1", "alice", "alice@test.com")
    session.add(u)
    session.flush()
    return u


@pytest.fixture()
def bob(session) -> User:
    u = _make_user("user_2", "bob", "bob@test.com")
    session.add(u)
    session.flush()
    return u


@pytest.fixture(autouse=True)
def _override_deps(session, master_key, alice):
    """Override FastAPI deps: DB session + current authenticated user."""
    # Ensure settings cache uses our test community key
    from config import get_settings
    get_settings.cache_clear()
    os.environ["COMMUNITY_ENCRYPTION_KEY"] = _TEST_COMMUNITY_KEY

    def _get_session():
        return session

    def _get_user():
        return alice

    def _get_master_key():
        return master_key

    app.dependency_overrides.clear()
    from database import get_session as _db_get_session
    from services.auth import get_current_user, get_master_key as _get_mk
    app.dependency_overrides[_db_get_session] = _get_session
    app.dependency_overrides[get_current_user] = _get_user
    app.dependency_overrides[_get_mk] = _get_master_key

    yield

    app.dependency_overrides.clear()
    get_settings.cache_clear()


# ── Community settings ─────────────────────────────────────────────────────────

class TestCommunitySettings:

    def test_get_settings_default_when_no_profile(self, session):
        """GET /community/settings returns safe defaults when user has no profile."""
        client = TestClient(app)
        r = client.get("/community/settings")
        assert r.status_code == 200
        data = r.json()
        assert data["is_active"] is False
        assert data["is_private"] is True
        assert data["display_name"] is None
        assert data["shared_stock_isins"] == []
        assert data["shared_crypto_symbols"] == []
        assert data["positions_count"] == 0

    def test_update_settings_activates_profile(self, session):
        """PUT /community/settings creates and activates a profile."""
        client = TestClient(app)
        r = client.put("/community/settings", json={
            "is_active": True,
            "is_private": False,
            "display_name": "Alice D.",
            "bio": "Investor since 2020",
            "shared_stock_isins": [],
            "shared_crypto_symbols": [],
        })
        assert r.status_code == 200
        data = r.json()
        assert data["is_active"] is True
        assert data["is_private"] is False
        assert data["display_name"] == "Alice D."
        assert data["positions_count"] == 0

    def test_update_settings_persists(self, session):
        """PUT then GET returns the same values."""
        client = TestClient(app)
        client.put("/community/settings", json={
            "is_active": True,
            "is_private": True,
            "display_name": "Alice",
            "bio": "My bio",
            "shared_stock_isins": [],
            "shared_crypto_symbols": [],
        })
        r = client.get("/community/settings")
        assert r.status_code == 200
        data = r.json()
        assert data["is_active"] is True
        assert data["is_private"] is True
        assert data["display_name"] == "Alice"

    def test_update_settings_deactivates_profile(self, session):
        """PUT is_active=False deactivates an existing profile."""
        client = TestClient(app)
        # Activate first
        client.put("/community/settings", json={
            "is_active": True, "is_private": False,
            "shared_stock_isins": [], "shared_crypto_symbols": [],
        })
        # Deactivate
        r = client.put("/community/settings", json={
            "is_active": False, "is_private": False,
            "shared_stock_isins": [], "shared_crypto_symbols": [],
        })
        assert r.status_code == 200
        assert r.json()["is_active"] is False


# ── Picks ──────────────────────────────────────────────────────────────────────

class TestPicks:

    def test_get_my_picks_initially_empty(self):
        """GET /community/picks/me returns empty list when user has no picks."""
        client = TestClient(app)
        r = client.get("/community/picks/me")
        assert r.status_code == 200
        assert r.json() == []

    def test_create_pick_requires_active_profile(self, session):
        """POST /community/picks fails with 400 if no active community profile."""
        client = TestClient(app)
        r = client.post("/community/picks", json={
            "symbol": "BTC",
            "asset_type": "CRYPTO",
        })
        # Service raises ValueError → route converts to 400
        assert r.status_code == 400
        assert "profil communautaire" in r.json()["detail"].lower()

    def test_create_pick_success(self, session):
        """POST /community/picks creates a pick after activating profile."""
        client = TestClient(app)
        # Activate profile first
        _activate_profile(session, "user_1", is_private=False)

        r = client.post("/community/picks", json={
            "symbol": "btc",          # should be uppercased
            "asset_type": "CRYPTO",
            "comment": "Bull run 2025",
            "target_price": 150000.0,
        })
        assert r.status_code == 201
        data = r.json()
        assert data["symbol"] == "BTC"         # uppercased
        assert data["asset_type"] == "CRYPTO"
        assert data["comment"] == "Bull run 2025"
        assert data["target_price"] == 150000.0
        assert data["username"] == "alice"
        assert "id" in data

    def test_create_pick_duplicate_rejected(self, session):
        """POST /community/picks with same symbol+type returns 400."""
        client = TestClient(app)
        _activate_profile(session, "user_1", is_private=False)

        client.post("/community/picks", json={"symbol": "AAPL", "asset_type": "STOCK"})
        r2 = client.post("/community/picks", json={"symbol": "AAPL", "asset_type": "STOCK"})
        assert r2.status_code == 400

    def test_create_pick_same_symbol_different_type_allowed(self, session):
        """POST allows same symbol for different asset types (BTC as STOCK vs CRYPTO)."""
        client = TestClient(app)
        _activate_profile(session, "user_1", is_private=False)

        r1 = client.post("/community/picks", json={"symbol": "BTC", "asset_type": "CRYPTO"})
        r2 = client.post("/community/picks", json={"symbol": "BTC", "asset_type": "STOCK"})
        assert r1.status_code == 201
        assert r2.status_code == 201

    def test_get_my_picks_after_create(self, session):
        """GET /community/picks/me reflects created picks."""
        client = TestClient(app)
        _activate_profile(session, "user_1", is_private=False)

        client.post("/community/picks", json={"symbol": "ETH", "asset_type": "CRYPTO"})
        client.post("/community/picks", json={"symbol": "MSFT", "asset_type": "STOCK"})

        r = client.get("/community/picks/me")
        assert r.status_code == 200
        picks = r.json()
        assert len(picks) == 2
        symbols = {p["symbol"] for p in picks}
        assert symbols == {"ETH", "MSFT"}

    def test_update_pick_comment_and_price(self, session):
        """PUT /community/picks/{id} updates comment and target_price."""
        client = TestClient(app)
        _activate_profile(session, "user_1", is_private=False)

        create_r = client.post("/community/picks", json={
            "symbol": "NVDA",
            "asset_type": "STOCK",
            "comment": "AI play",
            "target_price": 1000.0,
        })
        pick_id = create_r.json()["id"]

        r = client.put(f"/community/picks/{pick_id}", json={
            "comment": "Updated: still bullish",
            "target_price": 1500.0,
        })
        assert r.status_code == 200
        data = r.json()
        assert data["comment"] == "Updated: still bullish"
        assert data["target_price"] == 1500.0
        assert data["symbol"] == "NVDA"   # symbol unchanged

    def test_update_pick_not_found(self):
        """PUT /community/picks/9999 returns 400 for a non-existent pick."""
        client = TestClient(app)
        r = client.put("/community/picks/9999", json={"comment": None, "target_price": None})
        assert r.status_code == 400

    def test_delete_pick(self, session):
        """DELETE /community/picks/{id} removes the pick; subsequent list is empty."""
        client = TestClient(app)
        _activate_profile(session, "user_1", is_private=False)

        create_r = client.post("/community/picks", json={"symbol": "SOL", "asset_type": "CRYPTO"})
        pick_id = create_r.json()["id"]

        r = client.delete(f"/community/picks/{pick_id}")
        assert r.status_code == 204

        picks = client.get("/community/picks/me").json()
        assert picks == []

    def test_delete_pick_not_found(self):
        """DELETE /community/picks/9999 returns 404 for non-existent pick."""
        client = TestClient(app)
        r = client.delete("/community/picks/9999")
        assert r.status_code == 404

    def test_delete_clears_duplicate_constraint(self, session):
        """After deleting a pick, the same symbol+type can be re-created."""
        client = TestClient(app)
        _activate_profile(session, "user_1", is_private=False)

        r1 = client.post("/community/picks", json={"symbol": "ADA", "asset_type": "CRYPTO"})
        pick_id = r1.json()["id"]
        client.delete(f"/community/picks/{pick_id}")

        r2 = client.post("/community/picks", json={"symbol": "ADA", "asset_type": "CRYPTO"})
        assert r2.status_code == 201


# ── Follow / Unfollow ──────────────────────────────────────────────────────────

class TestFollow:

    def test_follow_user_with_no_active_profile_rejected(self, session, bob):
        """POST /community/follow/bob fails if bob has no active community profile."""
        client = TestClient(app)
        # bob exists but has no active community profile
        r = client.post("/community/follow/bob")
        assert r.status_code == 400

    def test_follow_self_rejected(self, session):
        """POST /community/follow/alice fails (can't follow yourself)."""
        client = TestClient(app)
        r = client.post("/community/follow/alice")
        assert r.status_code == 400

    def test_follow_nonexistent_user_rejected(self):
        """POST /community/follow/nobody returns 400 for unknown username."""
        client = TestClient(app)
        r = client.post("/community/follow/nobody")
        assert r.status_code == 400

    def test_follow_success(self, session, bob):
        """POST /community/follow/bob succeeds when bob has an active profile."""
        client = TestClient(app)
        _activate_profile(session, "user_2", is_private=False)

        r = client.post("/community/follow/bob")
        assert r.status_code == 200
        data = r.json()
        assert data["is_following"] is True
        assert data["is_mutual"] is False   # bob hasn't followed alice back

    def test_follow_already_following_is_idempotent(self, session, bob):
        """POST /community/follow/bob twice returns the same state without error."""
        client = TestClient(app)
        _activate_profile(session, "user_2", is_private=False)

        client.post("/community/follow/bob")
        r = client.post("/community/follow/bob")
        assert r.status_code == 200
        assert r.json()["is_following"] is True

    def test_unfollow_success(self, session, bob):
        """DELETE /community/follow/bob sets is_following=False."""
        client = TestClient(app)
        _activate_profile(session, "user_2", is_private=False)

        client.post("/community/follow/bob")
        r = client.delete("/community/follow/bob")
        assert r.status_code == 200
        data = r.json()
        assert data["is_following"] is False
        assert data["is_mutual"] is False

    def test_unfollow_when_not_following_is_idempotent(self, session, bob):
        """DELETE /community/follow/bob when not following returns 200 without error."""
        client = TestClient(app)
        r = client.delete("/community/follow/bob")
        assert r.status_code == 200
        assert r.json()["is_following"] is False

    def test_mutual_follow_detected(self, session, bob):
        """is_mutual=True when both users follow each other."""
        client = TestClient(app)
        _activate_profile(session, "user_2", is_private=False)
        _activate_profile(session, "user_1", is_private=False)

        # alice follows bob
        client.post("/community/follow/bob")

        # bob follows alice back (insert directly since we're authenticated as alice)
        follow_back = CommunityFollow(follower_id="user_2", following_id="user_1")
        session.add(follow_back)
        session.flush()

        # Now alice following again is idempotent → should detect mutual
        r = client.post("/community/follow/bob")
        assert r.status_code == 200
        assert r.json()["is_mutual"] is True


# ── Profile listing ────────────────────────────────────────────────────────────

class TestProfileListing:

    def test_list_profiles_empty_when_no_others(self, session):
        """GET /community/profiles returns [] when no other active profiles exist."""
        client = TestClient(app)
        r = client.get("/community/profiles")
        assert r.status_code == 200
        assert r.json() == []

    def test_list_profiles_shows_public_profiles(self, session, bob):
        """GET /community/profiles includes public active profiles."""
        client = TestClient(app)
        _activate_profile(session, "user_2", is_private=False, display_name="Bob D.")

        r = client.get("/community/profiles")
        assert r.status_code == 200
        profiles = r.json()
        assert len(profiles) == 1
        assert profiles[0]["username"] == "bob"
        assert profiles[0]["display_name"] == "Bob D."

    def test_list_profiles_excludes_own_profile(self, session):
        """GET /community/profiles never returns the current user's own profile."""
        client = TestClient(app)
        _activate_profile(session, "user_1", is_private=False)

        r = client.get("/community/profiles")
        assert r.status_code == 200
        usernames = [p["username"] for p in r.json()]
        assert "alice" not in usernames

    def test_list_profiles_excludes_private_not_followed(self, session, bob):
        """Private profiles of users alice doesn't follow are hidden from the list."""
        client = TestClient(app)
        _activate_profile(session, "user_2", is_private=True)   # private, not followed

        r = client.get("/community/profiles")
        assert r.status_code == 200
        assert r.json() == []

    def test_list_profiles_includes_private_if_followed(self, session, bob):
        """Private profiles appear in the list if alice already follows them."""
        client = TestClient(app)
        _activate_profile(session, "user_2", is_private=True)

        # alice follows bob
        follow = CommunityFollow(follower_id="user_1", following_id="user_2")
        session.add(follow)
        session.flush()

        r = client.get("/community/profiles")
        assert r.status_code == 200
        profiles = r.json()
        assert len(profiles) == 1
        assert profiles[0]["username"] == "bob"
        assert profiles[0]["is_following"] is True

    def test_list_profiles_followed_first(self, session, bob):
        """Followed profiles appear before non-followed ones in the list."""
        client = TestClient(app)
        charlie = _make_user("user_3", "charlie", "charlie@test.com")
        session.add(charlie)
        session.flush()

        _activate_profile(session, "user_2", is_private=False)   # bob
        _activate_profile(session, "user_3", is_private=False)   # charlie

        # alice follows bob (not charlie)
        follow = CommunityFollow(follower_id="user_1", following_id="user_2")
        session.add(follow)
        session.flush()

        r = client.get("/community/profiles")
        assert r.status_code == 200
        profiles = r.json()
        assert len(profiles) == 2
        # bob (followed) must come first
        assert profiles[0]["username"] == "bob"
        assert profiles[1]["username"] == "charlie"


# ── Profile detail ─────────────────────────────────────────────────────────────

class TestProfileDetail:

    def test_get_profile_not_found_for_inactive_user(self, session, bob):
        """GET /community/profiles/bob returns 404 if bob's profile is inactive."""
        client = TestClient(app)
        # bob exists but no active community profile
        r = client.get("/community/profiles/bob")
        assert r.status_code == 404

    def test_get_profile_not_found_for_unknown_username(self):
        """GET /community/profiles/nobody returns 404."""
        client = TestClient(app)
        r = client.get("/community/profiles/nobody")
        assert r.status_code == 404

    def test_get_public_profile_structure(self, session, bob):
        """GET /community/profiles/bob returns full profile for a public profile."""
        client = TestClient(app)
        _activate_profile(session, "user_2", is_private=False, display_name="Bob")

        r = client.get("/community/profiles/bob")
        assert r.status_code == 200
        data = r.json()
        assert data["username"] == "bob"
        assert data["display_name"] == "Bob"
        assert data["is_private"] is False
        assert data["is_following"] is False
        assert data["is_mutual"] is False
        assert isinstance(data["positions"], list)
        assert isinstance(data["picks"], list)

    def test_get_private_profile_not_mutual_hides_positions(self, session, bob):
        """GET private profile when not mutual: positions=[] and global_pnl=None."""
        client = TestClient(app)
        from services.encryption import community_encrypt

        profile = _activate_profile(session, "user_2", is_private=True)

        # Add a CommunityPosition to bob's profile
        pos = CommunityPosition(
            profile_user_id="user_2",
            asset_type="STOCK",
            symbol_encrypted=community_encrypt("AAPL"),
            pru_encrypted=community_encrypt("150.0"),
        )
        session.add(pos)
        session.flush()

        r = client.get("/community/profiles/bob")
        assert r.status_code == 200
        data = r.json()
        # Private + not mutual → positions must be hidden
        assert data["positions"] == []
        assert data["global_pnl_percentage"] is None
        assert data["is_following"] is False
        assert data["is_mutual"] is False

    def test_get_private_profile_mutual_shows_positions(self, session, bob):
        """GET private profile when mutually following: positions visible (pnl=None, no market data)."""
        client = TestClient(app)
        from services.encryption import community_encrypt

        _activate_profile(session, "user_2", is_private=True)
        _activate_profile(session, "user_1", is_private=True)

        # Add a CommunityPosition to bob's profile
        pos = CommunityPosition(
            profile_user_id="user_2",
            asset_type="STOCK",
            symbol_encrypted=community_encrypt("AAPL"),
            pru_encrypted=community_encrypt("150.0"),
        )
        session.add(pos)
        session.flush()

        # Create mutual follow
        session.add(CommunityFollow(follower_id="user_1", following_id="user_2"))
        session.add(CommunityFollow(follower_id="user_2", following_id="user_1"))
        session.flush()

        r = client.get("/community/profiles/bob")
        assert r.status_code == 200
        data = r.json()
        assert data["is_mutual"] is True
        # Positions should be visible (decoded symbol = "AAPL")
        assert len(data["positions"]) == 1
        assert data["positions"][0]["symbol"] == "AAPL"
        assert data["positions"][0]["asset_type"] == "STOCK"
        # pnl_percentage is None because there's no market data in test
        assert data["positions"][0]["pnl_percentage"] is None

    def test_profile_picks_are_visible(self, session, bob):
        """GET profile includes bob's picks regardless of privacy."""
        client = TestClient(app)
        _activate_profile(session, "user_2", is_private=False)
        _activate_profile(session, "user_1", is_private=False)

        # bob creates a pick directly in DB
        from models.community import CommunityPick
        pick = CommunityPick(
            user_id="user_2",
            symbol="BTC",
            asset_type="CRYPTO",
            comment="To the moon",
        )
        session.add(pick)
        session.flush()

        r = client.get("/community/profiles/bob")
        assert r.status_code == 200
        picks = r.json()["picks"]
        assert len(picks) == 1
        assert picks[0]["symbol"] == "BTC"
        assert picks[0]["username"] == "bob"


# ── Search ────────────────────────────────────────────────────────────────────

class TestSearch:

    def test_search_empty_query_returns_empty(self):
        """GET /community/search?q= (empty) returns []."""
        client = TestClient(app)
        r = client.get("/community/search?q=")
        assert r.status_code == 200
        assert r.json() == []

    def test_search_public_profile_partial_match(self, session, bob):
        """GET /community/search?q=bo finds bob (public, partial match)."""
        client = TestClient(app)
        _activate_profile(session, "user_2", is_private=False)

        r = client.get("/community/search?q=bo")
        assert r.status_code == 200
        results = r.json()
        assert len(results) == 1
        assert results[0]["username"] == "bob"

    def test_search_private_profile_not_found_on_partial_match(self, session, bob):
        """GET /community/search?q=bo does NOT return bob when his profile is private."""
        client = TestClient(app)
        _activate_profile(session, "user_2", is_private=True)

        r = client.get("/community/search?q=bo")
        assert r.status_code == 200
        assert r.json() == []

    def test_search_private_profile_found_on_exact_match(self, session, bob):
        """GET /community/search?q=bob DOES return bob (private) on exact match."""
        client = TestClient(app)
        _activate_profile(session, "user_2", is_private=True)

        r = client.get("/community/search?q=bob")
        assert r.status_code == 200
        results = r.json()
        assert len(results) == 1
        assert results[0]["username"] == "bob"
        assert results[0]["is_private"] is True

    def test_search_excludes_own_profile(self, session):
        """GET /community/search?q=alice never returns the current user."""
        client = TestClient(app)
        _activate_profile(session, "user_1", is_private=False)

        r = client.get("/community/search?q=alice")
        assert r.status_code == 200
        usernames = [u["username"] for u in r.json()]
        assert "alice" not in usernames

    def test_search_returns_follow_state(self, session, bob):
        """Search results include correct is_following flag."""
        client = TestClient(app)
        _activate_profile(session, "user_2", is_private=False)

        # Before following
        r1 = client.get("/community/search?q=bob")
        assert r1.json()[0]["is_following"] is False

        # After following
        session.add(CommunityFollow(follower_id="user_1", following_id="user_2"))
        session.flush()

        r2 = client.get("/community/search?q=bob")
        assert r2.json()[0]["is_following"] is True

    def test_search_inactive_profile_excluded(self, session, bob):
        """Inactive profiles (is_active=False) are not returned in search."""
        client = TestClient(app)
        # bob has a profile but it's inactive
        session.add(CommunityProfile(user_id="user_2", is_active=False, is_private=False))
        session.flush()

        r = client.get("/community/search?q=bob")
        assert r.status_code == 200
        assert r.json() == []
