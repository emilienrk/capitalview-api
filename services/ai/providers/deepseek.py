"""
DeepSeek provider via OpenAI-compatible API.

DeepSeek exposes an OpenAI-compatible endpoint, so we use the `openai` SDK.
Install: pip install openai
"""

from typing import Any

from openai import AsyncOpenAI

from services.ai.providers.base import AIProvider, ModelCapability


DEFAULT_MODEL = "deepseek-chat"
DEFAULT_MAX_TOKENS = 2000
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"


class DeepseekProvider(AIProvider):
    """
    DeepSeek provider using the OpenAI-compatible REST API.

    Capabilities: TEXT only (no vision on deepseek-chat; deepseek-reasoner adds REASONING).
    Tool format : OpenAI-style function calling (converted from Anthropic-style input_schema).
    """

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ):
        self.client = AsyncOpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL)
        self.model = model
        self.max_tokens = max_tokens

    # ------------------------------------------------------------------
    # Capabilities
    # ------------------------------------------------------------------

    def capabilities(self) -> ModelCapability:
        if "reasoner" in self.model.lower():
            return ModelCapability.TEXT | ModelCapability.REASONING
        return ModelCapability.TEXT

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
        """
        Convert canonical Anthropic-style messages to OpenAI format and call DeepSeek.
        """
        # DeepSeek API does not natively support strict JSON schemas via response_format.
        # We enforce it by ensuring response_format={"type": "json_object"} is enabled,
        # and appending the JSON schema definition to the system prompt.
        if output_config and "format" in output_config:
            fmt = output_config["format"]
            if fmt.get("type") == "json_schema" and "schema" in fmt:
                import json
                schema_str = json.dumps(fmt["schema"], ensure_ascii=False, indent=2)
                schema_instruction = f"\n\nJSON Output Schema to strictly follow:\n{schema_str}"
                if system:
                    system = system + schema_instruction
                else:
                    system = schema_instruction

        openai_messages = self._convert_messages(messages, system)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": openai_messages,
        }

        if tools:
            # If tools are already formatted in OpenAI function format, use as-is
            if isinstance(tools[0], dict) and tools[0].get("type") == "function" and "function" in tools[0]:
                kwargs["tools"] = tools
            else:
                kwargs["tools"] = self._convert_tools(tools)
            kwargs["tool_choice"] = "auto"

        if output_config and "format" in output_config:
            fmt = output_config["format"]
            if fmt.get("type") == "json_schema":
                kwargs["response_format"] = {"type": "json_object"}

        return await self.client.chat.completions.create(**kwargs)

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def extract_text(self, response: Any) -> str:
        try:
            return response.choices[0].message.content or ""
        except (AttributeError, IndexError):
            return ""

    def extract_tool_uses(self, response: Any) -> list[dict[str, Any]]:
        results = []
        try:
            tool_calls = response.choices[0].message.tool_calls or []
            for tc in tool_calls:
                import json
                try:
                    args = json.loads(tc.function.arguments)
                except (json.JSONDecodeError, TypeError):
                    args = {}
                results.append(
                    {"id": tc.id, "name": tc.function.name, "input": args}
                )
        except (AttributeError, IndexError):
            pass
        return results

    def extract_stop_reason(self, response: Any) -> str:
        try:
            finish_reason = response.choices[0].finish_reason
            if finish_reason == "tool_calls":
                return "tool_use"
            return "end_turn"
        except (AttributeError, IndexError):
            return "end_turn"

    def build_assistant_message(self, response: Any) -> dict[str, Any]:
        """
        Store the OpenAI message object for proper history reconstruction.
        """
        msg = response.choices[0].message
        # Convert back to a dict the converter can handle
        content_blocks = []
        if msg.content:
            content_blocks.append({"type": "text", "text": msg.content})
        if msg.tool_calls:
            for tc in msg.tool_calls:
                import json
                try:
                    args = json.loads(tc.function.arguments)
                except (json.JSONDecodeError, TypeError):
                    args = {}
                content_blocks.append(
                    {
                        "type": "tool_use",
                        "_openai_id": tc.id,  # keep the original ID for tool results
                        "name": tc.function.name,
                        "input": args,
                    }
                )
        return {"role": "assistant", "content": content_blocks}

    def build_tool_result_block(self, tool_use_id: str, content: str) -> dict[str, Any]:
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": content,
        }

    def format_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return self._convert_tools(tools)

    # ------------------------------------------------------------------
    # Internal conversion helpers
    # ------------------------------------------------------------------

    def _convert_messages(
        self, messages: list[dict[str, Any]], system: str | None
    ) -> list[dict[str, Any]]:
        """Convert canonical messages to OpenAI chat format."""
        openai_msgs: list[dict[str, Any]] = []

        if system:
            openai_msgs.append({"role": "system", "content": system})

        for msg in messages:
            role = msg["role"]
            content = msg["content"]

            if isinstance(content, str):
                openai_msgs.append({"role": role, "content": content})
                continue

            if isinstance(content, list):
                # Check for tool results (must be separate "tool" role messages in OpenAI)
                tool_results = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
                non_tool = [b for b in content if not (isinstance(b, dict) and b.get("type") == "tool_result")]

                if tool_results:
                    for tr in tool_results:
                        openai_msgs.append(
                            {
                                "role": "tool",
                                "tool_call_id": tr["tool_use_id"],
                                "content": tr["content"],
                            }
                        )

                if non_tool:
                    # Build text + image parts for the user/assistant message
                    parts = []
                    tool_calls_openai = []
                    for block in non_tool:
                        if isinstance(block, dict):
                            btype = block.get("type")
                            if btype == "text":
                                parts.append({"type": "text", "text": block["text"]})
                            elif btype == "image":
                                src = block.get("source", {})
                                if src.get("type") == "base64":
                                    parts.append(
                                        {
                                            "type": "image_url",
                                            "image_url": {
                                                "url": f"data:{src['media_type']};base64,{src['data']}"
                                            },
                                        }
                                    )
                            elif btype == "tool_use":
                                import json
                                tool_calls_openai.append(
                                    {
                                        "id": block.get("_openai_id", block.get("id", block["name"])),
                                        "type": "function",
                                        "function": {
                                            "name": block["name"],
                                            "arguments": json.dumps(block.get("input", {})),
                                        },
                                    }
                                )
                        elif hasattr(block, "type"):
                            # Anthropic SDK objects
                            if block.type == "text":
                                parts.append({"type": "text", "text": block.text})
                            elif block.type == "tool_use":
                                import json
                                tool_calls_openai.append(
                                    {
                                        "id": block.id,
                                        "type": "function",
                                        "function": {
                                            "name": block.name,
                                            "arguments": json.dumps(dict(block.input or {})),
                                        },
                                    }
                                )

                    out_msg: dict[str, Any] = {"role": role}
                    if tool_calls_openai:
                        out_msg["tool_calls"] = tool_calls_openai
                        out_msg["content"] = parts[0]["text"] if parts else None
                    elif parts:
                        out_msg["content"] = parts if len(parts) > 1 else parts[0].get("text", "")
                    openai_msgs.append(out_msg)

        return openai_msgs

    def _convert_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert canonical Anthropic-style tool defs to OpenAI function-calling format."""
        return [
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {}),
                },
            }
            for tool in tools
        ]