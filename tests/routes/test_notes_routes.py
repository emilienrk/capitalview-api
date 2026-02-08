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


def test_notes_crud(session, master_key):
    client = TestClient(app)

    r = client.post("/notes", json={"name": "T", "description": "C"})
    assert r.status_code == 201
    note = r.json()
    nid = note["id"]

    r2 = client.get("/notes")
    assert r2.status_code == 200
    assert any(n["id"] == nid for n in r2.json())

    r3 = client.get(f"/notes/{nid}")
    assert r3.status_code == 200

    r4 = client.put(f"/notes/{nid}", json={"content": "New"})
    assert r4.status_code == 200

    r5 = client.delete(f"/notes/{nid}")
    assert r5.status_code == 204
