"""
Google Gemini provider via the `google-genai` SDK.

Install: pip install google-genai
"""

from typing import Any

from google import genai
from google.genai import types as genai_types

from services.ai.providers.base import AIProvider, ModelCapability


DEFAULT_MODEL = "gemini-3.5-flash"
DEFAULT_MAX_TOKENS = 4000

def _clean_schema_for_gemini(schema: Any) -> Any:
    """
    Recursively clean a standard JSON schema to make it compatible with Gemini SDK's Schema.
    - Converts type lists like ['string', 'null'] to a single type with 'nullable': True.
    - Standardizes type values to uppercase (e.g. OBJECT, ARRAY, STRING).
    - Deletes 'additionalProperties' and 'additional_properties' which the Gemini API rejects.
    """
    if not isinstance(schema, dict):
        return schema

    cleaned = dict(schema)

    # Delete additionalProperties/additional_properties keys
    if "additionalProperties" in cleaned:
        del cleaned["additionalProperties"]
    if "additional_properties" in cleaned:
        del cleaned["additional_properties"]

    # 1. Handle type as a list (e.g., ['string', 'null'])
    type_val = cleaned.get("type")
    if isinstance(type_val, list):
        # Find the main type (the first non-null type)
        non_null_types = [t for t in type_val if t != "null"]
        if non_null_types:
            cleaned["type"] = non_null_types[0]
            cleaned["nullable"] = True
        else:
            cleaned["type"] = "null"
            cleaned["nullable"] = True

    # 2. Convert type to uppercase if it's a string
    if isinstance(cleaned.get("type"), str):
        cleaned["type"] = cleaned["type"].upper()

    # 3. Clean properties of objects recursively
    properties = cleaned.get("properties")
    if isinstance(properties, dict):
        cleaned["properties"] = {
            k: _clean_schema_for_gemini(v) for k, v in properties.items()
        }

    # 4. Clean items of arrays recursively
    items = cleaned.get("items")
    if isinstance(items, dict):
        cleaned["items"] = _clean_schema_for_gemini(items)
    elif isinstance(items, list):
        cleaned["items"] = [_clean_schema_for_gemini(i) for i in items]

    return cleaned


class GoogleProvider(AIProvider):
    """
    Google Gemini provider.

    Capabilities: TEXT + VISION
    Tool format : Google Function Declarations (converted from Anthropic-style input).
    """

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ):
        self.client = genai.Client(api_key=api_key)
        self.model = model
        self.max_tokens = max_tokens
        # Internal state to reconstruct conversation
        self._last_response: Any = None

    # ------------------------------------------------------------------
    # Capabilities
    # ------------------------------------------------------------------

    def capabilities(self) -> ModelCapability:
        return ModelCapability.TEXT | ModelCapability.VISION

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
        Convert the canonical message format to Gemini's Content format and call the API.

        The canonical format uses Anthropic-style messages:
            {"role": "user"|"assistant", "content": str | list[block]}
        """
        gemini_contents = self._convert_messages(messages)
        config_kwargs: dict[str, Any] = {"max_output_tokens": self.max_tokens}

        if system:
            config_kwargs["system_instruction"] = system

        if tools:
            # If tools are already formatted as Gemini Tool objects, use as-is
            if all(isinstance(t, genai_types.Tool) for t in tools):
                config_kwargs["tools"] = tools
            else:
                config_kwargs["tools"] = self._convert_tools(tools)

        # Structured output via response_schema when output_config is provided
        if output_config and "format" in output_config:
            fmt = output_config["format"]
            if fmt.get("type") == "json_schema":
                config_kwargs["response_mime_type"] = "application/json"
                if "schema" in fmt:
                    config_kwargs["response_schema"] = _clean_schema_for_gemini(fmt["schema"])

        config = genai_types.GenerateContentConfig(**config_kwargs)

        response = await self.client.aio.models.generate_content(
            model=self.model,
            contents=gemini_contents,
            config=config,
        )
        self._last_response = response
        return response

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def extract_text(self, response: Any) -> str:
        try:
            return response.text or ""
        except Exception:
            # response.text raises if the response contains only function calls
            for part in response.candidates[0].content.parts:
                if hasattr(part, "text") and part.text:
                    return part.text
        return ""

    def extract_tool_uses(self, response: Any) -> list[dict[str, Any]]:
        results = []
        try:
            for part in response.candidates[0].content.parts:
                if part.function_call:
                    fc = part.function_call
                    results.append(
                        {
                            "id": fc.name,  # Gemini has no separate ID; use name
                            "name": fc.name,
                            "input": dict(fc.args),
                        }
                    )
        except (AttributeError, IndexError):
            pass
        return results

    def extract_stop_reason(self, response: Any) -> str:
        try:
            finish_reason = response.candidates[0].finish_reason
            # Gemini finish reasons: STOP, MAX_TOKENS, SAFETY, RECITATION, OTHER, FUNCTION_CALLS (SDK-level)
            # The SDK exposes finish_reason as a string or FinishReason enum
            reason_str = str(finish_reason).upper()
            if "FUNCTION_CALL" in reason_str or "TOOL" in reason_str:
                return "tool_use"
            # Check if there are any function calls in the parts
            parts = response.candidates[0].content.parts
            if any(getattr(p, "function_call", None) for p in parts):
                return "tool_use"
            return "end_turn"
        except (AttributeError, IndexError):
            return "end_turn"

    def build_assistant_message(self, response: Any) -> dict[str, Any]:
        """
        Store the raw Gemini response as the assistant message.
        We attach it as a special dict for use in _convert_messages.
        """
        return {"role": "assistant", "content": response.candidates[0].content}

    def build_tool_result_block(self, tool_use_id: str, content: str) -> dict[str, Any]:
        """
        For Gemini, tool results are function responses.
        tool_use_id is the function name (since Gemini has no separate call ID).
        """
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

    def _convert_messages(self, messages: list[dict[str, Any]]) -> list[Any]:
        """Convert canonical messages to Gemini Content objects."""
        gemini_contents = []
        for msg in messages:
            role = msg["role"]
            content = msg["content"]
            gemini_role = "user" if role == "user" else "model"

            # Already a Gemini Content object (stored by build_assistant_message)
            if hasattr(content, "parts"):
                gemini_contents.append(
                    genai_types.Content(role=gemini_role, parts=content.parts)
                )
                continue

            parts: list[Any] = []

            if isinstance(content, str):
                parts.append(genai_types.Part(text=content))

            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        block_type = block.get("type")

                        if block_type == "text":
                            parts.append(genai_types.Part(text=block["text"]))

                        elif block_type == "image":
                            src = block.get("source", {})
                            if src.get("type") == "base64":
                                import base64
                                parts.append(
                                    genai_types.Part(
                                        inline_data=genai_types.Blob(
                                            mime_type=src["media_type"],
                                            data=base64.b64decode(src["data"]),
                                        )
                                    )
                                )

                        elif block_type == "tool_result":
                            import json
                            tool_use_id = block.get("tool_use_id", "")
                            # Synthetic (pre-injected) tool results: convert to plain text
                            # to avoid Gemini's thought_signature requirement
                            if str(tool_use_id).startswith("toolu_fake"):
                                raw = block.get("content", "")
                                try:
                                    pretty = json.dumps(json.loads(raw), ensure_ascii=False, indent=2)
                                except (json.JSONDecodeError, TypeError):
                                    pretty = str(raw)
                                parts.append(genai_types.Part(
                                    text=f"[Données pré-chargées pour {tool_use_id}]:\n{pretty}"
                                ))
                            else:
                                # Real tool result → Gemini FunctionResponse
                                try:
                                    result_data = json.loads(block["content"])
                                    if not isinstance(result_data, dict):
                                        result_data = {"result": result_data}
                                except (json.JSONDecodeError, TypeError):
                                    result_data = {"result": block["content"]}
                                parts.append(
                                    genai_types.Part(
                                        function_response=genai_types.FunctionResponse(
                                            name=tool_use_id,
                                            response=result_data,
                                        )
                                    )
                                )

                        elif block_type == "tool_use":
                            tool_id = block.get("id", "")
                            # Synthetic tool_use: convert to plain text to avoid thought_signature
                            if str(tool_id).startswith("toolu_fake"):
                                parts.append(genai_types.Part(
                                    text=f"[Appel d'outil synthétique: {block.get('name', '')}]"
                                ))
                            else:
                                # Real tool_use block in assistant turn → FunctionCall
                                parts.append(
                                    genai_types.Part(
                                        function_call=genai_types.FunctionCall(
                                            name=block["name"],
                                            args=block.get("input", {}),
                                        )
                                    )
                                )

                    elif hasattr(block, "type"):
                        # Anthropic SDK objects (TextBlock, ToolUseBlock, etc.)
                        if block.type == "text":
                            parts.append(genai_types.Part(text=block.text))
                        elif block.type == "tool_use":
                            parts.append(
                                genai_types.Part(
                                    function_call=genai_types.FunctionCall(
                                        name=block.name,
                                        args=dict(block.input or {}),
                                    )
                                )
                            )

            if parts:
                gemini_contents.append(
                    genai_types.Content(role=gemini_role, parts=parts)
                )

        return gemini_contents

    def _convert_tools(self, tools: list[dict[str, Any]]) -> list[Any]:
        """Convert canonical Anthropic-style tool defs to Gemini FunctionDeclarations."""
        declarations = []
        for tool in tools:
            schema = tool.get("input_schema", {})
            declarations.append(
                genai_types.FunctionDeclaration(
                    name=tool["name"],
                    description=tool.get("description", ""),
                    parameters=schema,
                )
            )
        return [genai_types.Tool(function_declarations=declarations)]
