"""
OpenRouter provider: one key for the models of many vendors.

OpenRouter speaks the OpenAI chat-completions format DeepSeek speaks, so it
reuses that provider's conversions and only changes the endpoint, the model
catalogue and structured outputs.
"""

from services.ai.providers.base import DetectedModel, ModelCapability
from services.ai.providers.deepseek import DeepseekProvider

DEFAULT_MAX_TOKENS = 4000
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterProvider(DeepseekProvider):
    """
    OpenRouter provider using the OpenAI-compatible REST API.

    Capabilities: TEXT + VISION, the latter depending on the model: settle_model
    skips a model that reads no images for the photo import.
    Tool format : OpenAI-style function calling, as for DeepSeek.
    """

    provider_id = "openrouter"
    base_url = OPENROUTER_BASE_URL
    # Passed on to the models that take one; the others still see the schema
    # in the system prompt.
    native_json_schema = True

    def __init__(
        self,
        api_key: str,
        model: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ):
        super().__init__(api_key, model, max_tokens)

    def capabilities(self) -> ModelCapability:
        return ModelCapability.TEXT | ModelCapability.VISION

    async def list_models(self) -> list[DetectedModel]:
        models = []
        async for model in self.client.models.list():
            # OpenRouter's fields beyond the OpenAI shape land in model_extra
            extra = model.model_extra or {}
            architecture = extra.get("architecture") or {}
            # Both agents call tools: a model without them could only guess.
            if "tools" not in (extra.get("supported_parameters") or []):
                continue
            if "text" not in (architecture.get("output_modalities") or ["text"]):
                continue
            models.append(
                DetectedModel(
                    id=model.id,
                    label=extra.get("name") or model.id,
                    vision="image" in (architecture.get("input_modalities") or []),
                )
            )
        return models
