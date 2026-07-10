import pytest
from fastapi.testclient import TestClient

from main import app
from models.user import User


@pytest.fixture(autouse=True)
def _override_deps(session, master_key):
    def _get_session():
        return session

    app.dependency_overrides.clear()
    from database import get_session
    app.dependency_overrides[get_session] = _get_session

    # Reset the in-memory rate limiter between tests
    from routes.auth import _rate_hits
    _rate_hits.clear()

    yield

    app.dependency_overrides.clear()
    _rate_hits.clear()


def test_register_and_me(session):
    client = TestClient(app)

    # Test 1: Default behavior (no header) - master_key should be None
    payload = {"username": "user1", "email": "u1@example.com", "password": "Strongpass1!"}
    r = client.post("/auth/register", json=payload)
    assert r.status_code == 201
    data = r.json()
    assert "access_token" in data
    assert data.get("master_key") is None  # Not returned by default

    # Test 2: With opt-in header - master_key should be returned
    payload2 = {"username": "user2", "email": "u2@example.com", "password": "Strongpass2!"}
    r2 = client.post("/auth/register", json=payload2, headers={"X-Return-Master-Key": "true"})
    assert r2.status_code == 201
    data2 = r2.json()
    assert "access_token" in data2
    assert "master_key" in data2
    assert data2["master_key"] is not None  # Returned when requested

    token = data["access_token"]
    r3 = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r3.status_code == 200
    me = r3.json()
    assert me["username"] == "user1"


def test_login_refresh_and_logout(session):
    client = TestClient(app)

    # Register user
    payload = {"username": "loginuser", "email": "login@example.com", "password": "StrongLogin1!"}
    r = client.post("/auth/register", json=payload)
    assert r.status_code == 201

    # Test 1: Login without opt-in header - master_key should be None
    login = {"email": "login@example.com", "password": "StrongLogin1!"}
    r2 = client.post("/auth/login", json=login)
    assert r2.status_code == 200
    data = r2.json()
    assert "access_token" in data
    assert data.get("master_key") is None  # Not returned by default

    # Test 2: Login with opt-in header - master_key should be returned
    r2_optin = client.post("/auth/login", json=login, headers={"X-Return-Master-Key": "true"})
    assert r2_optin.status_code == 200
    data_optin = r2_optin.json()
    assert "access_token" in data_optin
    assert "master_key" in data_optin
    assert data_optin["master_key"] is not None  # Returned when requested

    cookies = r2.cookies
    assert "refresh_token" in cookies, "Login must set a refresh_token cookie"
    r3 = client.post("/auth/refresh")
    assert r3.status_code == 200
    assert "access_token" in r3.json()

    token = data["access_token"]
    r4 = client.post("/auth/logout", headers={"Authorization": f"Bearer {token}"})
    assert r4.status_code == 200
    assert r4.json().get("message") == "Logged out successfully"


def test_register_uses_random_wrapped_master_key(session):
    """New accounts get a random MK, wrapped — not the legacy password-derived MK."""
    from sqlmodel import select
    from services.encryption import get_masterkey, unwrap_master_key

    client = TestClient(app)
    payload = {"username": "wrapuser", "email": "wrap@example.com", "password": "Strongpass1!"}
    r = client.post("/auth/register", json=payload, headers={"X-Return-Master-Key": "true"})
    assert r.status_code == 201
    master_key = r.json()["master_key"]

    user = session.exec(select(User).where(User.email == "wrap@example.com")).first()
    assert user.mk_wrapped_password is not None
    assert user.mk_salt_password is not None
    # MK is random, not the legacy derivation
    assert master_key != get_masterkey("Strongpass1!", user.auth_salt)
    # Wrapped MK unwraps to the same MK
    assert unwrap_master_key(user.mk_wrapped_password, "Strongpass1!", user.mk_salt_password) == master_key


def test_change_password_keeps_data_readable(session):
    """Core invariant: the MK never changes, so encrypted data survives a password change."""
    client = TestClient(app)
    payload = {"username": "pwchange", "email": "pwchange@example.com", "password": "OldPass123!"}
    r = client.post("/auth/register", json=payload)
    assert r.status_code == 201
    token = r.json()["access_token"]
    auth = {"Authorization": f"Bearer {token}"}

    # Create encrypted data (bank account) with the register session cookies
    r_acc = client.post(
        "/bank/accounts",
        json={"name": "Compte Test", "account_type": "CHECKING", "balance": "1234.56"},
        headers=auth,
    )
    assert r_acc.status_code == 201

    # Change password
    r_pw = client.put(
        "/auth/me/password",
        json={"current_password": "OldPass123!", "new_password": "NewPass456!"},
        headers=auth,
    )
    assert r_pw.status_code == 200
    new_token = r_pw.json()["access_token"]

    # Old password rejected, new one accepted
    assert client.post("/auth/login", json={"email": "pwchange@example.com", "password": "OldPass123!"}).status_code == 401
    r_login = client.post("/auth/login", json={"email": "pwchange@example.com", "password": "NewPass456!"})
    assert r_login.status_code == 200

    # Encrypted data still readable after re-login
    r_read = client.get("/bank/accounts", headers={"Authorization": f"Bearer {r_login.json()['access_token']}"})
    assert r_read.status_code == 200
    accounts = r_read.json()["accounts"]
    assert len(accounts) == 1
    assert accounts[0]["name"] == "Compte Test"
    assert new_token


def test_change_password_wrong_current_password(session):
    client = TestClient(app)
    payload = {"username": "pwwrong", "email": "pwwrong@example.com", "password": "OldPass123!"}
    r = client.post("/auth/register", json=payload)
    auth = {"Authorization": f"Bearer {r.json()['access_token']}"}

    r_pw = client.put(
        "/auth/me/password",
        json={"current_password": "WrongPass1!", "new_password": "NewPass456!"},
        headers=auth,
    )
    assert r_pw.status_code == 401


def test_change_password_revokes_old_refresh_tokens(session):
    client = TestClient(app)
    payload = {"username": "pwrevoke", "email": "pwrevoke@example.com", "password": "OldPass123!"}
    r = client.post("/auth/register", json=payload)
    auth = {"Authorization": f"Bearer {r.json()['access_token']}"}
    old_refresh = r.cookies["refresh_token"]

    r_pw = client.put(
        "/auth/me/password",
        json={"current_password": "OldPass123!", "new_password": "NewPass456!"},
        headers=auth,
    )
    assert r_pw.status_code == 200

    # The pre-change refresh token no longer works
    client.cookies.clear()
    client.cookies.set("refresh_token", old_refresh)
    assert client.post("/auth/refresh").status_code == 401


def test_legacy_login_lazy_migration(session):
    """A legacy account (no wrapped MK) keeps its derived MK and gets wrapped at login."""
    import uuid as uuid_mod
    from sqlmodel import select
    from services.encryption import get_masterkey, hash_password, init_salt

    auth_salt = init_salt()
    legacy = User(
        uuid=str(uuid_mod.uuid4()),
        username="legacyuser",
        email="legacy@example.com",
        auth_salt=auth_salt,
        password_hash=hash_password("LegacyPass1!"),
    )
    session.add(legacy)
    session.commit()
    legacy_mk = get_masterkey("LegacyPass1!", auth_salt)

    client = TestClient(app)
    login = {"email": "legacy@example.com", "password": "LegacyPass1!"}
    r = client.post("/auth/login", json=login, headers={"X-Return-Master-Key": "true"})
    assert r.status_code == 200
    # Same MK as before the migration — data stays readable
    assert r.json()["master_key"] == legacy_mk

    user = session.exec(select(User).where(User.email == "legacy@example.com")).first()
    session.refresh(user)
    assert user.mk_wrapped_password is not None

    # Second login goes through the unwrap path and returns the same MK
    r2 = client.post("/auth/login", json=login, headers={"X-Return-Master-Key": "true"})
    assert r2.status_code == 200
    assert r2.json()["master_key"] == legacy_mk
