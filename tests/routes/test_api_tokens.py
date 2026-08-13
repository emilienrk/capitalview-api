"""API token routes: minting, listing and revoking from the settings UI."""

import pytest
from fastapi.testclient import TestClient

from main import app
from services import api_token as api_token_service


@pytest.fixture(autouse=True)
def _override_deps(session):
    def _get_session():
        return session

    app.dependency_overrides.clear()
    from database import get_session

    app.dependency_overrides[get_session] = _get_session

    from routes.auth import _rate_hits

    _rate_hits.clear()

    yield

    app.dependency_overrides.clear()
    _rate_hits.clear()


PASSWORD = "StrongToken1!"


def _register(client: TestClient, email: str = "token@example.com") -> tuple[str, str]:
    """Register a user and return (access token, master key)."""
    payload = {"username": email.split("@")[0], "email": email, "password": PASSWORD}
    response = client.post("/auth/register", json=payload, headers={"X-Return-Master-Key": "true"})
    assert response.status_code == 201
    body = response.json()
    return body["access_token"], body["master_key"]


def _auth_headers(access_token: str, master_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}", "X-Master-Key": master_key}


def test_create_token_returns_the_secret_once(session):
    client = TestClient(app)
    access_token, master_key = _register(client)

    response = client.post(
        "/auth/tokens",
        json={"password": PASSWORD, "name": "Claude Desktop"},
        headers=_auth_headers(access_token, master_key),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["token"].startswith(api_token_service.TOKEN_PREFIX)
    assert body["name"] == "Claude Desktop"
    assert body["scopes"] == ["read"]

    # The listing must never hand the secret back.
    listed = client.get("/auth/tokens", headers=_auth_headers(access_token, master_key))
    assert listed.status_code == 200
    assert [t["name"] for t in listed.json()] == ["Claude Desktop"]
    assert "token" not in listed.json()[0]


def test_create_token_requires_the_password(session):
    client = TestClient(app)
    access_token, master_key = _register(client, "wrongpass@example.com")

    response = client.post(
        "/auth/tokens",
        json={"password": "NotThePassword1!", "name": "Nope"},
        headers=_auth_headers(access_token, master_key),
    )

    assert response.status_code == 401
    listed = client.get("/auth/tokens", headers=_auth_headers(access_token, master_key))
    assert listed.json() == []


def test_create_token_requires_authentication(session):
    client = TestClient(app)

    response = client.post("/auth/tokens", json={"password": PASSWORD, "name": "Anon"})

    assert response.status_code in (401, 403)


def test_revoke_token_removes_it_from_the_list(session):
    client = TestClient(app)
    access_token, master_key = _register(client, "revoke@example.com")
    headers = _auth_headers(access_token, master_key)

    created = client.post(
        "/auth/tokens", json={"password": PASSWORD, "name": "Doomed"}, headers=headers
    ).json()

    response = client.delete(f"/auth/tokens/{created['uuid']}", headers=headers)

    assert response.status_code == 204
    assert client.get("/auth/tokens", headers=headers).json() == []


def test_revoking_an_unknown_token_is_a_404(session):
    client = TestClient(app)
    access_token, master_key = _register(client, "unknown@example.com")

    response = client.delete(
        "/auth/tokens/does-not-exist", headers=_auth_headers(access_token, master_key)
    )

    assert response.status_code == 404


def test_a_user_cannot_see_or_revoke_another_users_tokens(session):
    client = TestClient(app)
    alice_token, alice_key = _register(client, "alice@example.com")
    bob_token, bob_key = _register(client, "bob@example.com")

    alice_created = client.post(
        "/auth/tokens",
        json={"password": PASSWORD, "name": "Alice's"},
        headers=_auth_headers(alice_token, alice_key),
    ).json()

    assert client.get("/auth/tokens", headers=_auth_headers(bob_token, bob_key)).json() == []

    response = client.delete(
        f"/auth/tokens/{alice_created['uuid']}", headers=_auth_headers(bob_token, bob_key)
    )
    assert response.status_code == 404

    # Alice's token survived Bob's attempt.
    assert len(client.get("/auth/tokens", headers=_auth_headers(alice_token, alice_key)).json()) == 1


def test_token_count_is_capped_per_account(session, monkeypatch):
    monkeypatch.setattr("routes.api_tokens.MAX_TOKENS_PER_USER", 2)
    client = TestClient(app)
    access_token, master_key = _register(client, "capped@example.com")
    headers = _auth_headers(access_token, master_key)

    for index in range(2):
        assert (
            client.post(
                "/auth/tokens", json={"password": PASSWORD, "name": f"t{index}"}, headers=headers
            ).status_code
            == 201
        )

    response = client.post(
        "/auth/tokens", json={"password": PASSWORD, "name": "one too many"}, headers=headers
    )

    assert response.status_code == 409


def test_expiry_is_carried_through(session):
    client = TestClient(app)
    access_token, master_key = _register(client, "expiring@example.com")

    response = client.post(
        "/auth/tokens",
        json={"password": PASSWORD, "name": "Temporary", "expires_in_days": 30},
        headers=_auth_headers(access_token, master_key),
    )

    assert response.status_code == 201
    assert response.json()["expires_at"] is not None


def test_mcp_connection_details_are_exposed_to_the_ui(session):
    client = TestClient(app)
    access_token, master_key = _register(client, "mcpinfo@example.com")

    response = client.get("/auth/tokens/mcp", headers=_auth_headers(access_token, master_key))

    assert response.status_code == 200
    body = response.json()
    assert body["transport"] == "streamable-http"
    assert body["url"]
