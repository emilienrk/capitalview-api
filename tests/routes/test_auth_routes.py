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

    yield

    app.dependency_overrides.clear()


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
    if "refresh_token" in cookies:
        r3 = client.post("/auth/refresh")
        assert r3.status_code == 200
        assert "access_token" in r3.json()

    token = data["access_token"]
    r4 = client.post("/auth/logout", headers={"Authorization": f"Bearer {token}"})
    assert r4.status_code == 200
    assert r4.json().get("message") == "Logged out successfully"
