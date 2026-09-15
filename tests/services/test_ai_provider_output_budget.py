"""
A caller can raise a provider's output budget for one call. The SDK clients are
stubbed: no network call is ever made.
"""
import asyncio
from types import SimpleNamespace

import pytest

from services.ai.providers.anthropic import AnthropicProvider
from services.ai.providers.deepseek import DeepseekProvider
from services.ai.providers.google import GoogleProvider


class _Recorder:
    def __init__(self):
        self.kwargs: dict = {}

    async def __call__(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace()


def _anthropic(recorder: _Recorder) -> AnthropicProvider:
    provider = AnthropicProvider(api_key="test")
    provider.client = SimpleNamespace(messages=SimpleNamespace(create=recorder))
    return provider


def _deepseek(recorder: _Recorder) -> DeepseekProvider:
    provider = DeepseekProvider(api_key="test")
    provider.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=recorder)))
    return provider


def _google(recorder: _Recorder) -> GoogleProvider:
    provider = GoogleProvider(api_key="test")
    provider.client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=recorder)))
    return provider


def _sent_budget(provider, recorder: _Recorder) -> int:
    kwargs = recorder.kwargs
    return kwargs["config"].max_output_tokens if isinstance(provider, GoogleProvider) else kwargs["max_tokens"]


@pytest.mark.parametrize("build", [_anthropic, _deepseek, _google])
def test_the_call_s_budget_overrides_the_provider_s_default(build):
    recorder = _Recorder()
    provider = build(recorder)
    messages = [{"role": "user", "content": "hello"}]

    asyncio.run(provider._send_message(messages=messages))
    assert _sent_budget(provider, recorder) == provider.max_tokens

    asyncio.run(provider._send_message(messages=messages, max_tokens=8000))
    assert _sent_budget(provider, recorder) == 8000
