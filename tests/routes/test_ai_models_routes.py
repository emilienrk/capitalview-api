"""GET /settings/ai/providers/{provider}/models and the model a user may pick."""

from unittest.mock import AsyncMock, patch

import httpx
import openai
import pytest
from fastapi.testclient import TestClient

from main import app
from models.user import User
from services.ai import catalog
from services.ai.providers.base import DetectedModel
from services.ai.providers.openrouter import OpenRouterProvider


@pytest.fixture(autouse=True)
def _override_deps(session, master_key, monkeypatch):
    from database import get_session
    from services.auth import get_current_user, get_master_key

    monkeypatch.setattr(catalog, "_cache", {})
    app.dependency_overrides.clear()
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_current_user] = lambda: User(
        uuid="user_1", auth_salt="salt", username="test", email="t@test", password_hash="x"
    )
    app.dependency_overrides[get_master_key] = lambda: master_key
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


LISTED = [
    DetectedModel("qwen/qwen3-max", "Qwen: Qwen3 Max", vision=False),
    DetectedModel("google/gemini-3.5-flash", "Google: Gemini 3.5 Flash", vision=True),
]


def test_lists_the_models_the_key_reaches(client):
    assert client.put("/settings/ai/providers/openrouter", json={"api_key": "sk-or-v1-test"}).status_code == 200

    with patch.object(OpenRouterProvider, "list_models", AsyncMock(return_value=LISTED)):
        r = client.get("/settings/ai/providers/openrouter/models")

    assert r.status_code == 200
    assert r.json() == {
        "models": [
            {"id": "qwen/qwen3-max", "label": "Qwen: Qwen3 Max", "vision": False},
            {"id": "google/gemini-3.5-flash", "label": "Google: Gemini 3.5 Flash", "vision": True},
        ],
        "recommended": "google/gemini-3.5-flash",
    }


def test_listing_asks_the_provider_again_each_time(client):
    client.put("/settings/ai/providers/openrouter", json={"api_key": "sk-or-v1-test"})

    with patch.object(OpenRouterProvider, "list_models", AsyncMock(return_value=LISTED)) as listed:
        client.get("/settings/ai/providers/openrouter/models")
        client.get("/settings/ai/providers/openrouter/models")

    assert listed.await_count == 2


def test_listing_needs_a_key(client):
    r = client.get("/settings/ai/providers/openrouter/models")

    assert r.status_code == 400
    assert "clé API" in r.json()["detail"]


def test_listing_an_unknown_provider(client):
    assert client.get("/settings/ai/providers/nope/models").status_code == 404


def test_a_refused_key_names_openrouter(client):
    client.put("/settings/ai/providers/openrouter", json={"api_key": "sk-or-v1-bad"})
    request = httpx.Request("GET", "https://openrouter.ai/api/v1/models")
    error = openai.AuthenticationError(
        "No auth credentials found", response=httpx.Response(401, request=request), body=None
    )

    with patch.object(OpenRouterProvider, "list_models", AsyncMock(side_effect=error)):
        r = client.get("/settings/ai/providers/openrouter/models")

    assert r.status_code == 503
    assert r.json()["detail"].startswith("Fournisseur OpenRouter : clé API refusée")


@pytest.mark.parametrize(
    "model", ["meta-llama/llama-3.1-8b-instruct:free", "gemini-3.5-flash", "claude-sonnet-4-6"]
)
def test_any_well_formed_model_id_can_be_chosen(client, model):
    r = client.put("/settings/ai/providers/openrouter", json={"selected_model": model})

    assert r.status_code == 200
    assert r.json()["selected_model"] == model


def test_a_malformed_model_id_is_refused(client):
    r = client.put("/settings/ai/providers/openrouter", json={"selected_model": "not a model"})

    assert r.status_code == 400


def test_options_offer_openrouter_for_both_uses(client):
    r = client.get("/settings/ai/options")

    assert r.status_code == 200
    capabilities = r.json()["capabilities"]
    assert "openrouter" in [o["provider"] for o in capabilities["vision"]]
    assert "openrouter" in [o["provider"] for o in capabilities["chat"]]
    assert all("models" not in o for o in capabilities["chat"])
