from abc import ABC, abstractmethod
from enum import Flag, auto, Enum
from typing import Any


class ModelCapability(Flag):
    """Bitflag representing what a provider/model can do."""
    TEXT = auto()
    VISION = auto()   # image understanding
    REASONING = auto()  # extended thinking / o1-style reasoning


class ProviderType(str, Enum):
    """Identifies an AI provider backend."""
    ANTHROPIC = "anthropic"
    GOOGLE = "google"
    DEEPSEEK = "deepseek"


class AIProvider(ABC):
    """
    Abstract base class for all AI providers.

    Subclasses must implement:
    - `capabilities()` → ModelCapability  (what this model supports)
    - `_send_message(...)` → raw provider response
    - `extract_text(response)` → str  (parse text from response)
    - `extract_tool_uses(response)` → list of tool use dicts
    - `extract_stop_reason(response)` → str  ("end_turn" | "tool_use" | ...)
    - `build_tool_result_block(tool_use_id, content)` → dict
    - `format_tools(tools)` → provider-specific tool list
    """

    @abstractmethod
    def capabilities(self) -> ModelCapability:
        """Return the capabilities supported by this provider/model."""

    def supports(self, required: ModelCapability) -> bool:
        """Return True if all required capabilities are met."""
        return (self.capabilities() & required) == required

    # ------------------------------------------------------------------
    # Core messaging — must be implemented by each provider
    # ------------------------------------------------------------------

    @abstractmethod
    async def _send_message(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
        output_config: dict[str, Any] | None = None,
    ) -> Any:
        """Send a message to the underlying API and return the raw response."""

    # ------------------------------------------------------------------
    # Response parsing helpers — must be implemented by each provider
    # ------------------------------------------------------------------

    @abstractmethod
    def extract_text(self, response: Any) -> str:
        """Extract the main text content from a raw provider response."""

    @abstractmethod
    def extract_tool_uses(self, response: Any) -> list[dict[str, Any]]:
        """
        Extract tool-use blocks from a response.

        Each returned dict must have the shape:
            {"id": str, "name": str, "input": dict}
        """

    @abstractmethod
    def extract_stop_reason(self, response: Any) -> str:
        """
        Return a normalised stop reason string.

        Convention used across agents:
            "end_turn"   — model finished normally
            "tool_use"   — model wants to call a tool
        """

    @abstractmethod
    def build_assistant_message(self, response: Any) -> dict[str, Any]:
        """
        Convert a raw response into the assistant message dict
        to append to the conversation history.
        """

    @abstractmethod
    def build_tool_result_block(self, tool_use_id: str, content: str) -> dict[str, Any]:
        """
        Build a single tool-result block in the format expected by this provider.
        """

    @abstractmethod
    def format_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        Convert generic tool definitions (Anthropic-style) into the format
        expected by this provider's API.

        The canonical input format is:
            {
                "name": str,
                "description": str,
                "input_schema": { "type": "object", "properties": {...}, ... }
            }
        """