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


def test_update_display_timezone(session, master_key):
    client = TestClient(app)

    # Default: no preference (follow the browser)
    r = client.get("/settings")
    assert r.status_code == 200
    assert r.json()["display_timezone"] is None

    # Set a valid IANA timezone and check persistence
    r2 = client.put("/settings", json={"display_timezone": "Europe/Paris"})
    assert r2.status_code == 200
    assert r2.json()["display_timezone"] == "Europe/Paris"
    assert client.get("/settings").json()["display_timezone"] == "Europe/Paris"

    # Invalid timezone is rejected without clobbering the stored value
    r3 = client.put("/settings", json={"display_timezone": "Mars/Olympus_Mons"})
    assert r3.status_code == 400
    assert client.get("/settings").json()["display_timezone"] == "Europe/Paris"

    # Explicit null resets to browser default
    r4 = client.put("/settings", json={"display_timezone": None})
    assert r4.status_code == 200
    assert r4.json()["display_timezone"] is None


def test_update_display_locale(session, master_key):
    client = TestClient(app)

    # Default: app default (fr-FR handled client-side)
    assert client.get("/settings").json()["display_locale"] is None

    # Set a supported locale and check persistence
    r = client.put("/settings", json={"display_locale": "en-GB"})
    assert r.status_code == 200
    assert r.json()["display_locale"] == "en-GB"
    assert client.get("/settings").json()["display_locale"] == "en-GB"

    # Unsupported locale rejected without clobbering the stored value
    r2 = client.put("/settings", json={"display_locale": "xx-XX"})
    assert r2.status_code == 400
    assert client.get("/settings").json()["display_locale"] == "en-GB"

    # Explicit null resets to the app default
    r3 = client.put("/settings", json={"display_locale": None})
    assert r3.status_code == 200
    assert r3.json()["display_locale"] is None


def test_update_ai_api_keys(session, master_key):
    client = TestClient(app)

    # 1. Update settings
    r = client.put("/settings", json={
        "ai_feature_enabled": True,
    })
    assert r.status_code == 200
    data = r.json()
    assert data["ai_feature_enabled"] is True

    # Update providers
    r_claude = client.put("/settings/ai/providers/anthropic", json={
        "api_key": "sk-ant-test-key"
    })
    assert r_claude.status_code == 200
    assert r_claude.json()["has_key"] is True

    r_deepseek = client.put("/settings/ai/providers/deepseek", json={
        "api_key": "sk-deepseek-test-key"
    })
    assert r_deepseek.status_code == 200
    assert r_deepseek.json()["has_key"] is True

    r_gemini = client.put("/settings/ai/providers/google", json={
        "api_key": "AIzaSy-test-key"
    })
    assert r_gemini.status_code == 200
    assert r_gemini.json()["has_key"] is True

    # 2. Verify settings retrieval shows the keys are present
    r2 = client.get("/settings")
    assert r2.status_code == 200
    data2 = r2.json()
    providers = {p["provider"]: p for p in data2["ai_providers"]}
    assert providers["anthropic"]["has_key"] is True
    assert providers["deepseek"]["has_key"] is True
    assert providers["google"]["has_key"] is True

    # 3. Check DB storage is encrypted
    from models.user import UserAIProvider
    from services.encryption import decrypt_data, hash_index
    from sqlmodel import select
    
    user_bidx = hash_index("user_1", master_key)
    providers_db = session.exec(
        select(UserAIProvider).where(UserAIProvider.user_uuid_bidx == user_bidx)
    ).all()
    providers_dict = {p.provider: p for p in providers_db}
    
    anthropic_db = providers_dict["anthropic"]
    assert anthropic_db.api_key_enc is not None
    assert anthropic_db.api_key_enc != "sk-ant-test-key"
    assert decrypt_data(anthropic_db.api_key_enc, master_key) == "sk-ant-test-key"

    deepseek_db = providers_dict["deepseek"]
    assert decrypt_data(deepseek_db.api_key_enc, master_key) == "sk-deepseek-test-key"

    google_db = providers_dict["google"]
    assert decrypt_data(google_db.api_key_enc, master_key) == "AIzaSy-test-key"

    # 4. Clear one key and check
    r3 = client.put("/settings/ai/providers/anthropic", json={
        "api_key": None
    })
    assert r3.status_code == 200
    assert r3.json()["has_key"] is False
    
    r4 = client.get("/settings")
    assert r4.status_code == 200
    data4 = r4.json()
    providers4 = {p["provider"]: p for p in data4["ai_providers"]}
    assert providers4["anthropic"]["has_key"] is False
    assert providers4["deepseek"]["has_key"] is True
