import pytest
from fastapi.testclient import TestClient

from main import app
from models.user import User


@pytest.fixture(autouse=True)
def _override_deps(session, master_key):
    def _get_session():
        return session

    def _get_user():
        return User(uuid="user_1", auth_salt="salt", username="test", email="t@test", password_hash="x")

    def _get_master_key():
        return master_key

    app.dependency_overrides.clear()
    from database import get_session
    app.dependency_overrides[get_session] = _get_session
    try:
        from services.auth import get_current_user, get_master_key
        app.dependency_overrides[get_current_user] = _get_user
        app.dependency_overrides[get_master_key] = _get_master_key
    except Exception:
        pass

    yield

    app.dependency_overrides.clear()


def test_get_settings(session, master_key):
    client = TestClient(app)

    r = client.get("/settings")
    assert r.status_code == 200
    data = r.json()
    assert "crypto_show_negative_positions" in data
    assert data["crypto_show_negative_positions"] is False
    assert data["crypto_module_enabled"] is False


def test_update_settings(session, master_key):
    client = TestClient(app)

    # Enable module and set negative positions to true
    r = client.put("/settings", json={
        "crypto_module_enabled": True,
        "crypto_show_negative_positions": True,
        "crypto_mode": "MULTI"
    })
    assert r.status_code == 200
    data = r.json()
    assert data["crypto_module_enabled"] is True
    assert data["crypto_show_negative_positions"] is True
    assert data["crypto_mode"] == "MULTI"

    # Verify changes persisted
    r2 = client.get("/settings")
    assert r2.status_code == 200
    data2 = r2.json()
    assert data2["crypto_module_enabled"] is True
    assert data2["crypto_show_negative_positions"] is True
    assert data2["crypto_mode"] == "MULTI"

    # Set negative positions back to false
    r3 = client.put("/settings", json={
        "crypto_show_negative_positions": False,
    })
    assert r3.status_code == 200
    data3 = r3.json()
    assert data3["crypto_module_enabled"] is True  # Should remain unchanged
    assert data3["crypto_show_negative_positions"] is False
