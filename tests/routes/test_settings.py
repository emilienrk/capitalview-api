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


def test_update_ai_api_keys(session, master_key):
    client = TestClient(app)

    # 1. Update settings with keys
    r = client.put("/settings", json={
        "ai_feature_enabled": True,
        "claude_api_key": "sk-ant-test-key",
        "deepseek_api_key": "sk-deepseek-test-key",
        "gemini_api_key": "AIzaSy-test-key"
    })
    assert r.status_code == 200
    data = r.json()
    assert data["ai_feature_enabled"] is True
    assert data["has_claude_api_key"] is True
    assert data["has_deepseek_api_key"] is True
    assert data["has_gemini_api_key"] is True

    # Check that keys are not exposed in clear
    assert "claude_api_key" not in data
    assert "deepseek_api_key" not in data
    assert "gemini_api_key" not in data

    # 2. Verify settings retrieval shows the keys are present
    r2 = client.get("/settings")
    assert r2.status_code == 200
    data2 = r2.json()
    assert data2["has_claude_api_key"] is True
    assert data2["has_deepseek_api_key"] is True
    assert data2["has_gemini_api_key"] is True

    # 3. Check DB storage is encrypted
    from services.encryption import decrypt_data
    from services.settings import get_or_create_settings
    
    settings_db = get_or_create_settings(session, "user_1", master_key)
    assert settings_db.claude_api_key_enc is not None
    assert settings_db.claude_api_key_enc != "sk-ant-test-key"
    
    decrypted_claude = decrypt_data(settings_db.claude_api_key_enc, master_key)
    assert decrypted_claude == "sk-ant-test-key"

    decrypted_deepseek = decrypt_data(settings_db.deepseek_api_key_enc, master_key)
    assert decrypted_deepseek == "sk-deepseek-test-key"

    decrypted_gemini = decrypt_data(settings_db.gemini_api_key_enc, master_key)
    assert decrypted_gemini == "AIzaSy-test-key"

    # 4. Clear one key and check
    r3 = client.put("/settings", json={
        "claude_api_key": ""
    })
    assert r3.status_code == 200
    data3 = r3.json()
    assert data3["has_claude_api_key"] is False
    assert data3["has_deepseek_api_key"] is True
