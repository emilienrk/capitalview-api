import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Flag, auto, Enum
from typing import Any, ClassVar


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
    OPENROUTER = "openrouter"


@dataclass(frozen=True)
class DetectedModel:
    """A model the user's key can reach, as the provider lists it."""
    id: str
    label: str
    vision: bool  # reads images, so it can serve the photo import


class AIProvider(ABC):
    """
    Abstract base class for all AI providers.

    Subclasses must implement:
    - `capabilities()` → ModelCapability  (what this model supports)
    - `list_models()` → the models the API key can reach
    - `_send_message(...)` → raw provider response
    - `extract_text(response)` → str  (parse text from response)
    - `extract_tool_uses(response)` → list of tool use dicts
    - `extract_stop_reason(response)` → str  ("end_turn" | "tool_use" | ...)
    - `build_tool_result_block(tool_use_id, content)` → dict
    - `format_tools(tools)` → provider-specific tool list
    """

    provider_id: ClassVar[str]

    def __init__(self, api_key: str, model: str | None):
        # None = automatic: settled against the live model list before the
        # first call (services.ai.catalog.settle_model).
        self.model = model
        # Keys the detected-model cache without keeping the key itself around
        self.key_fingerprint = hashlib.sha256(api_key.encode()).hexdigest()

    @abstractmethod
    def capabilities(self) -> ModelCapability:
        """Return the capabilities supported by this provider/model."""

    @abstractmethod
    async def list_models(self) -> list[DetectedModel]:
        """Return the models the API key can reach and the agents can drive."""

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