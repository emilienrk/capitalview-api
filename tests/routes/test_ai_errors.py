"""A failed AI call answers with a detail the browser can read.

Every request carries an Origin: the bug these guard against is a response the
browser refuses for its missing Access-Control-Allow-Origin, which the frontend
only ever sees as "Failed to fetch".
"""

from types import SimpleNamespace
from unittest.mock import patch

import anthropic
import httpx
import openai
import pytest
from fastapi.testclient import TestClient
from google.genai import errors as genai_errors

from config import get_settings
from main import app
from models.enums import AssetType
from services.ai.agents.extract_tx_agent import ExtractTxAgent
from services.ai.manager import NoProviderAvailableError

ORIGIN = get_settings().cors_origins[0]
_REQUEST = httpx.Request("POST", "https://provider.example/v1/messages")


@pytest.fixture(autouse=True)
def _override_deps(session):
    from database import get_session

    app.dependency_overrides.clear()
    app.dependency_overrides[get_session] = lambda: session
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def client():
    # Do not re-raise: what matters is the response the browser would get.
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def headers(client):
    r = client.post(
        "/auth/register",
        json={"username": "aiuser", "email": "ai@example.com", "password": "StrongAi1!x"},
    )
    assert r.status_code == 201
    return {"Authorization": f"Bearer {r.json()['access_token']}", "Origin": ORIGIN}


def _extract(client, headers, error, path="/stocks/transactions/extract", module="routes.stocks"):
    with patch(f"{module}.ExtractTxAgent", side_effect=error):
        return client.post(path, files={"file": ("tx.png", b"img", "image/png")}, headers=headers)


def test_unhandled_error_still_carries_cors(client, headers):
    r = _extract(client, headers, RuntimeError("boom"))

    assert r.status_code == 500
    assert r.headers["access-control-allow-origin"] == ORIGIN
    assert r.json() == {"detail": "Erreur interne du serveur"}


def test_no_provider_asks_for_a_key(client, headers):
    r = _extract(client, headers, NoProviderAvailableError("none"))

    assert r.status_code == 400
    assert r.headers["access-control-allow-origin"] == ORIGIN
    assert "Aucun fournisseur IA" in r.json()["detail"]


@pytest.mark.parametrize(
    "error, provider, advice",
    [
        (
            anthropic.AuthenticationError(
                "invalid x-api-key", response=httpx.Response(401, request=_REQUEST), body=None
            ),
            "Claude (Anthropic)",
            "clé API refusée",
        ),
        (
            genai_errors.ClientError(
                404,
                {
                    "error": {
                        "code": 404,
                        "message": "models/gemini-x is not found",
                        "status": "NOT_FOUND",
                    }
                },
            ),
            "Gemini (Google)",
            "modèle introuvable",
        ),
        (
            genai_errors.ClientError(
                429,
                {
                    "error": {
                        "code": 429,
                        "message": "Quota exceeded",
                        "status": "RESOURCE_EXHAUSTED",
                    }
                },
            ),
            "Gemini (Google)",
            "quota atteint",
        ),
        (openai.APIConnectionError(request=_REQUEST), "DeepSeek", "service indisponible"),
    ],
)
def test_provider_error_names_provider_and_cause(client, headers, error, provider, advice):
    r = _extract(client, headers, error)

    assert r.status_code == 503
    assert r.headers["access-control-allow-origin"] == ORIGIN
    detail = r.json()["detail"]
    assert provider in detail
    assert advice in detail


def test_crypto_extract_answers_the_same_way(client, headers):
    error = genai_errors.ClientError(403, {"error": {"code": 403, "message": "API key not valid"}})
    r = _extract(client, headers, error, "/crypto/transactions/extract", "routes.crypto")

    assert r.status_code == 503
    assert r.headers["access-control-allow-origin"] == ORIGIN
    assert "clé API refusée" in r.json()["detail"]


def test_dashboard_card_provider_error_carries_cors(client, headers):
    error = genai_errors.ServerError(503, {"error": {"code": 503, "message": "overloaded"}})
    with patch("services.ai.agents.card_agent.CardAgent", side_effect=error):
        r = client.get("/dashboard/card", headers=headers)

    assert r.status_code == 503
    assert r.headers["access-control-allow-origin"] == ORIGIN
    assert "service indisponible" in r.json()["detail"]


class _TruncatingProvider:
    """Answers once with a truncated turn; a second call means the loop went on."""

    def __init__(self):
        self.calls = 0

    def format_tools(self, tools):
        return tools

    async def _send_message(self, **kwargs):
        self.calls += 1
        if self.calls > 1:
            raise AssertionError("resent a conversation ending on the model")
        return SimpleNamespace(stop_reason="max_tokens")

    def build_assistant_message(self, response):
        return {"role": "assistant", "content": [{"type": "text", "text": '{"transactions": ['}]}

    def extract_stop_reason(self, response):
        return response.stop_reason

    def extract_tool_uses(self, response):
        return []


async def test_extract_stops_on_a_truncated_answer(session, master_key):
    provider = _TruncatingProvider()
    with (
        patch("services.ai.agents.extract_tx_agent.AIProviderManager") as manager,
        patch("services.ai.agents.extract_tx_agent.get_all_assets", return_value=[]),
    ):
        manager.from_user_settings.return_value.get_provider_for_capability.return_value = provider
        agent = ExtractTxAgent("user-uuid", session, master_key)
        text = await agent.analyse("aW1n", "image/png", AssetType.STOCK)

    assert provider.calls == 1
    assert text == '{"transactions": ['
