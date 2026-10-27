"""Model detection: what each provider lists, and what "automatic" settles on."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import anthropic
import httpx
import httpx2
import openai
import pytest

from services.ai import catalog
from services.ai.catalog import detect_models, recommend, settle_model
from services.ai.manager import AIProviderManager, NoProviderAvailableError
from services.ai.providers.anthropic import AnthropicProvider
from services.ai.providers.base import DetectedModel, ModelCapability
from services.ai.providers.deepseek import DeepseekProvider
from services.ai.providers.google import GoogleProvider
from services.ai.providers.openrouter import OpenRouterProvider


@pytest.fixture(autouse=True)
def _empty_cache(monkeypatch):
    monkeypatch.setattr(catalog, "_cache", {})


def _models(*specs):
    """("id", vision) pairs → DetectedModel list."""
    return [DetectedModel(id=model_id, label=model_id, vision=vision) for model_id, vision in specs]


def _mock_http(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# recommend
# ---------------------------------------------------------------------------


def test_google_recommends_the_newest_stable_flash():
    models = _models(
        ("gemini-2.5-flash", True),
        ("gemini-3.5-flash", True),
        ("gemini-3.5-pro", True),
        ("gemini-4-flash-preview-09-2026", True),
        ("gemini-3.5-flash-lite", True),
    )
    assert recommend("google", models) == "gemini-3.5-flash"


def test_google_falls_back_to_a_preview_when_no_stable_flash_exists():
    models = _models(("gemini-4-flash-preview", True), ("gemini-2.5-flash-lite", True))
    assert recommend("google", models) == "gemini-4-flash-preview"


def test_anthropic_reads_a_dated_snapshot_as_its_version():
    # The date must not make 4.5 look newer than 4.6
    models = _models(
        ("claude-opus-4-8", True),
        ("claude-sonnet-4-5-20250929", True),
        ("claude-sonnet-4-6", True),
        ("claude-haiku-4-5", True),
    )
    assert recommend("anthropic", models) == "claude-sonnet-4-6"


def test_the_photo_import_keeps_to_models_that_read_images():
    models = _models(
        ("google/gemini-3.5-flash", False),
        ("anthropic/claude-sonnet-4.6", True),
    )
    assert recommend("openrouter", models, vision=True) == "anthropic/claude-sonnet-4.6"
    # The chat has no need for images
    assert recommend("openrouter", models) == "google/gemini-3.5-flash"


def test_no_photo_import_model_when_none_reads_images():
    models = _models(("mistralai/mistral-large", False), ("qwen/qwen3-max", False))
    assert recommend("openrouter", models, vision=True) is None
    assert recommend("openrouter", models) == "mistralai/mistral-large"


def test_deepseek_recommends_flash_for_both_uses():
    # What a DeepSeek key lists since the V4.1 renaming
    models = _models(("deepseek-flash", True), ("deepseek-v4-pro", False))
    assert recommend("deepseek", models, vision=True) == "deepseek-flash"
    assert recommend("deepseek", models) == "deepseek-flash"


def test_nothing_to_recommend_without_models():
    assert recommend("google", []) is None
    assert recommend("unknown", _models(("x", True))) is None


# ---------------------------------------------------------------------------
# detect_models / settle_model
# ---------------------------------------------------------------------------


class _FakeProvider(OpenRouterProvider):
    def __init__(self, models, model=None, api_key="sk-or-test"):
        super().__init__(api_key, model)
        self.list_models = AsyncMock(return_value=models)


async def test_detection_is_cached_until_asked_fresh():
    provider = _FakeProvider(_models(("google/gemini-3.5-flash", True)))

    await detect_models(provider)
    await detect_models(provider)
    assert provider.list_models.await_count == 1

    await detect_models(provider, fresh=True)
    assert provider.list_models.await_count == 2


async def test_detection_cache_is_per_key():
    first = _FakeProvider(_models(("a/one", True)), api_key="sk-or-first")
    second = _FakeProvider(_models(("b/two", True)), api_key="sk-or-second")

    assert [m.id for m in await detect_models(first)] == ["a/one"]
    assert [m.id for m in await detect_models(second)] == ["b/two"]


async def test_automatic_settles_on_the_recommended_model():
    provider = _FakeProvider(
        _models(("openai/gpt-5-mini", True), ("google/gemini-3.5-flash", True))
    )

    assert await settle_model(provider, ModelCapability.TEXT) is True
    assert provider.model == "google/gemini-3.5-flash"


async def test_a_chosen_model_is_kept():
    provider = _FakeProvider(_models(("google/gemini-3.5-flash", True)), model="openai/gpt-5")

    assert await settle_model(provider, ModelCapability.TEXT) is True
    assert provider.model == "openai/gpt-5"


async def test_a_chosen_model_that_reads_no_images_turns_the_photo_import_down():
    provider = _FakeProvider(_models(("qwen/qwen3-max", False)), model="qwen/qwen3-max")
    vision = ModelCapability.TEXT | ModelCapability.VISION

    assert await settle_model(provider, vision) is False
    assert await settle_model(provider, ModelCapability.TEXT) is True


async def test_a_failed_listing_falls_back_and_lets_the_call_report():
    provider = _FakeProvider([])
    provider.list_models = AsyncMock(side_effect=RuntimeError("network down"))

    assert await settle_model(provider, ModelCapability.TEXT) is True
    assert provider.model == "openrouter/auto"


async def test_deepseek_without_a_vision_model_turns_the_photo_import_down():
    provider = DeepseekProvider("sk-test")
    provider.list_models = AsyncMock(return_value=_models(("deepseek-v4-pro", False)))

    assert await settle_model(provider, ModelCapability.TEXT | ModelCapability.VISION) is False


async def test_each_use_calls_its_own_model():
    provider = DeepseekProvider("sk-test")
    provider.list_models = AsyncMock(
        return_value=_models(("deepseek-v4-flash", False), ("deepseek-v4-flash-vision-exp", True))
    )
    vision = ModelCapability.TEXT | ModelCapability.VISION

    assert await settle_model(provider, vision) is True
    assert provider.model == "deepseek-v4-flash-vision-exp"
    assert await settle_model(provider, ModelCapability.TEXT) is True
    assert provider.model == "deepseek-v4-flash"


async def test_a_chosen_vision_model_is_kept_for_the_photo_import_only():
    provider = _FakeProvider(
        _models(("google/gemini-3.5-flash", True), ("anthropic/claude-sonnet-4.6", True)),
        model="openai/gpt-5",
    )
    provider.vision_model = "anthropic/claude-sonnet-4.6"
    vision = ModelCapability.TEXT | ModelCapability.VISION

    assert await settle_model(provider, vision) is True
    assert provider.model == "anthropic/claude-sonnet-4.6"
    assert await settle_model(provider, ModelCapability.TEXT) is True
    assert provider.model == "openai/gpt-5"


async def test_a_failed_deepseek_listing_falls_back_on_flash():
    provider = DeepseekProvider("sk-test")
    provider.list_models = AsyncMock(side_effect=RuntimeError("network down"))

    assert await settle_model(provider, ModelCapability.TEXT | ModelCapability.VISION) is True
    assert provider.model == "deepseek-flash"


async def test_deepseek_serves_the_photo_import_when_preferred():
    deepseek = DeepseekProvider("sk-test")
    deepseek.list_models = AsyncMock(
        return_value=_models(("deepseek-v4-flash", False), ("deepseek-v4-flash-vision-exp", True))
    )
    claude = AnthropicProvider("sk-ant-test")
    claude.list_models = AsyncMock(return_value=_models(("claude-sonnet-4-6", True)))
    manager = AIProviderManager(
        {"deepseek": deepseek, "anthropic": claude}, vision_preference="deepseek"
    )

    assert await manager.get_provider_for_capability("vision") is deepseek
    assert deepseek.model == "deepseek-v4-flash-vision-exp"


async def test_vision_moves_on_from_a_text_only_openrouter_model():
    text_only = _FakeProvider(_models(("qwen/qwen3-max", False)), model="qwen/qwen3-max")
    claude = AnthropicProvider("sk-ant-test")
    claude.list_models = AsyncMock(return_value=_models(("claude-sonnet-4-6", True)))
    manager = AIProviderManager(
        {"openrouter": text_only, "anthropic": claude}, vision_preference="openrouter"
    )

    assert await manager.get_provider_for_capability("vision") is claude
    assert claude.model == "claude-sonnet-4-6"


async def test_no_provider_left_for_vision_raises():
    text_only = _FakeProvider(_models(("qwen/qwen3-max", False)), model="qwen/qwen3-max")
    manager = AIProviderManager({"openrouter": text_only})

    with pytest.raises(NoProviderAvailableError):
        await manager.get_provider_for_capability("vision")


# ---------------------------------------------------------------------------
# What each provider lists
# ---------------------------------------------------------------------------


async def test_openrouter_lists_models_that_call_tools():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/models"
        assert request.headers["authorization"] == "Bearer sk-or-test"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "google/gemini-3.5-flash",
                        "name": "Google: Gemini 3.5 Flash",
                        "architecture": {
                            "input_modalities": ["text", "image"],
                            "output_modalities": ["text"],
                        },
                        "supported_parameters": ["tools", "structured_outputs"],
                    },
                    {
                        "id": "qwen/qwen3-max",
                        "name": "Qwen: Qwen3 Max",
                        "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
                        "supported_parameters": ["tools"],
                    },
                    {
                        "id": "some/no-tools",
                        "name": "No tools",
                        "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
                        "supported_parameters": ["temperature"],
                    },
                    {
                        "id": "some/image-maker",
                        "name": "Image maker",
                        "architecture": {"input_modalities": ["text"], "output_modalities": ["image"]},
                        "supported_parameters": ["tools"],
                    },
                ]
            },
        )

    provider = OpenRouterProvider("sk-or-test")
    provider.client = openai.AsyncOpenAI(
        api_key="sk-or-test", base_url=provider.base_url, http_client=_mock_http(handler)
    )

    assert await provider.list_models() == [
        DetectedModel("google/gemini-3.5-flash", "Google: Gemini 3.5 Flash", vision=True),
        DetectedModel("qwen/qwen3-max", "Qwen: Qwen3 Max", vision=False),
    ]



async def test_deepseek_tells_its_image_readers_by_name():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {"id": model_id, "object": "model", "owned_by": "deepseek"}
                    for model_id in ("deepseek-flash", "deepseek-v4-pro", "deepseek-v4-flash-vision-exp", "deepseek-vl2")
                ],
            },
        )

    provider = DeepseekProvider("sk-test")
    provider.client = openai.AsyncOpenAI(
        api_key="sk-test", base_url=provider.base_url, http_client=_mock_http(handler)
    )

    assert [(m.id, m.vision) for m in await provider.list_models()] == [
        ("deepseek-flash", True),
        ("deepseek-v4-pro", False),
        ("deepseek-v4-flash-vision-exp", True),
        ("deepseek-vl2", True),
    ]

async def test_anthropic_lists_models_with_structured_outputs():
    def capabilities(structured: bool, images: bool) -> dict:
        support = lambda flag: {"supported": flag}  # noqa: E731
        return {
            "batch": support(True),
            "citations": support(True),
            "code_execution": support(True),
            "context_management": {"supported": True},
            "effort": {"supported": True},
            "image_input": support(images),
            "pdf_input": support(True),
            "structured_outputs": support(structured),
            "thinking": {"supported": True},
        }

    # The Anthropic SDK ships its own fork of httpx
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/v1/models"
        return httpx2.Response(
            200,
            json={
                "data": [
                    {
                        "type": "model",
                        "id": "claude-sonnet-4-6",
                        "display_name": "Claude Sonnet 4.6",
                        "created_at": "2026-02-17T00:00:00Z",
                        "capabilities": capabilities(structured=True, images=True),
                    },
                    {
                        "type": "model",
                        "id": "claude-3-haiku-20240307",
                        "display_name": "Claude Haiku 3",
                        "created_at": "2024-03-07T00:00:00Z",
                        "capabilities": capabilities(structured=False, images=True),
                    },
                ],
                "has_more": False,
                "first_id": "claude-sonnet-4-6",
                "last_id": "claude-3-haiku-20240307",
            },
        )

    provider = AnthropicProvider("sk-ant-test")
    provider.client = anthropic.AsyncAnthropic(
        api_key="sk-ant-test",
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )

    assert await provider.list_models() == [
        DetectedModel("claude-sonnet-4-6", "Claude Sonnet 4.6", vision=True)
    ]


async def test_google_lists_conversational_gemini_models():
    listed = [
        SimpleNamespace(name="models/gemini-3.5-flash", display_name="Gemini 3.5 Flash",
                        supported_actions=["generateContent", "countTokens"]),
        SimpleNamespace(name="models/gemini-2.5-flash-preview-tts", display_name="TTS",
                        supported_actions=["generateContent"]),
        SimpleNamespace(name="models/gemini-embedding-001", display_name="Embedding",
                        supported_actions=["embedContent"]),
        SimpleNamespace(name="models/imagen-4.0-generate-001", display_name="Imagen",
                        supported_actions=["predict"]),
        SimpleNamespace(name="models/gemini-3.5-flash-image", display_name="Image",
                        supported_actions=["generateContent"]),
    ]

    async def pager():
        for model in listed:
            yield model

    provider = GoogleProvider("AIza-test")
    with patch.object(provider.client.aio.models, "list", AsyncMock(return_value=pager())):
        assert await provider.list_models() == [
            DetectedModel("gemini-3.5-flash", "Gemini 3.5 Flash", vision=True)
        ]


# ---------------------------------------------------------------------------
# Structured outputs on the OpenAI-compatible wire
# ---------------------------------------------------------------------------


def _completion_capture(sent: list):
    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "gen-1",
                "object": "chat.completion",
                "created": 0,
                "model": "m",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "{}"},
                    }
                ],
            },
        )

    return handler


OUTPUT = {"format": {"type": "json_schema", "schema": {"type": "object", "properties": {}}}}


@pytest.mark.parametrize(
    "provider_class, expected",
    [(OpenRouterProvider, "json_schema"), (DeepseekProvider, "json_object")],
)
async def test_structured_output_format(provider_class, expected):
    sent: list = []
    provider = provider_class("sk-test", model="some/model")
    provider.client = openai.AsyncOpenAI(
        api_key="sk-test", base_url=provider.base_url, http_client=_mock_http(_completion_capture(sent))
    )

    await provider._send_message(messages=[{"role": "user", "content": "hi"}], output_config=OUTPUT)

    assert sent[0]["response_format"]["type"] == expected
    assert sent[0]["model"] == "some/model"


@pytest.mark.parametrize(
    "provider_class, thinking",
    [(DeepseekProvider, {"type": "disabled"}), (OpenRouterProvider, None)],
)
async def test_deepseek_calls_without_thinking(provider_class, thinking):
    # Thinking with tools wants every earlier assistant turn's reasoning back,
    # which the extraction's synthetic opening turn cannot give.
    sent: list = []
    provider = provider_class("sk-test", model="some/model")
    provider.client = openai.AsyncOpenAI(
        api_key="sk-test", base_url=provider.base_url, http_client=_mock_http(_completion_capture(sent))
    )

    await provider._send_message(messages=[{"role": "user", "content": "hi"}])

    assert sent[0].get("thinking") == thinking


def test_reasoning_goes_back_with_its_assistant_turn():
    provider = DeepseekProvider("sk-test", model="deepseek-flash")
    message = SimpleNamespace(
        content="Let me look it up.",
        reasoning_content="The ISIN is not on the screen.",
        tool_calls=[
            SimpleNamespace(
                id="call_1",
                function=SimpleNamespace(name="search_assets", arguments='{"query": "Amundi"}'),
            )
        ],
    )
    response = SimpleNamespace(choices=[SimpleNamespace(message=message)])

    stored = provider.build_assistant_message(response)
    # The agents read the answer from the first block
    assert stored["content"][0] == {"type": "text", "text": "Let me look it up."}

    [sent] = provider._convert_messages([stored], system=None)
    assert sent["reasoning_content"] == "The ISIN is not on the screen."
    assert sent["tool_calls"][0]["id"] == "call_1"
    assert sent["content"] == "Let me look it up."
