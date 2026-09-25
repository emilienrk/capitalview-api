from typing import Any

import anthropic

from services.ai.providers.base import AIProvider, DetectedModel, ModelCapability


DEFAULT_MAX_TOKENS = 2000


class AnthropicProvider(AIProvider):
    """
    Anthropic Claude provider.

    Capabilities: TEXT + VISION
    Tool format : native Anthropic tool schema (input_schema field).
    """

    provider_id = "anthropic"

    def __init__(
        self,
        api_key: str,
        model: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ):
        super().__init__(api_key, model)
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.max_tokens = max_tokens

    # ------------------------------------------------------------------
    # Capabilities
    # ------------------------------------------------------------------

    def capabilities(self) -> ModelCapability:
        return ModelCapability.TEXT | ModelCapability.VISION

    async def list_models(self) -> list[DetectedModel]:
        models = []
        # Newest first, as the API lists them
        async for info in self.client.models.list(limit=100):
            caps = info.capabilities
            # Both agents pass output_config: a model without structured
            # outputs would refuse every call.
            if caps is not None and not caps.structured_outputs.supported:
                continue
            models.append(
                DetectedModel(
                    id=info.id,
                    label=info.display_name or info.id,
                    vision=caps.image_input.supported if caps is not None else True,
                )
            )
        return models

    # ------------------------------------------------------------------
    # Core messaging
    # ------------------------------------------------------------------

    async def _send_message(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
        output_config: dict[str, Any] | None = None,
    ) -> Any:
        args: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": messages,
        }
        if tools:
            args["tools"] = tools
        if system:
            args["system"] = system
        if output_config:
            args["output_config"] = output_config
        return await self.client.messages.create(**args)

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def extract_text(self, response: Any) -> str:
        for block in response.content:
            if hasattr(block, "text"):
                return block.text
        return ""

    def extract_tool_uses(self, response: Any) -> list[dict[str, Any]]:
        results = []
        for block in response.content:
            if getattr(block, "type", None) == "tool_use":
                results.append(
                    {"id": block.id, "name": block.name, "input": block.input}
                )
        return results

    def extract_stop_reason(self, response: Any) -> str:
        # Anthropic uses "end_turn" and "tool_use" — already matches our convention
        return response.stop_reason or "end_turn"

    def build_assistant_message(self, response: Any) -> dict[str, Any]:
        return {"role": "assistant", "content": response.content}

    def build_tool_result_block(self, tool_use_id: str, content: str) -> dict[str, Any]:
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": content,
        }

    def format_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # Anthropic uses the canonical format natively — no conversion needed
        return tools
