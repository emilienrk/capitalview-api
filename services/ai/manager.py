"""
AI Provider Manager.

Resolves the best available AI provider for a given user and required capability.

Resolution order (configurable):
    1. Google (Gemini)     — TEXT + VISION
    2. DeepSeek            — TEXT (+ REASONING for deepseek-reasoner)
    3. Anthropic (Claude)  — TEXT + VISION

The manager reads the user's decrypted API keys from UserSettings and
instantiates only the providers for which the user has a valid key.
"""

from __future__ import annotations

import logging
from typing import Any

from services.ai.providers.base import AIProvider, ModelCapability
from services.ai.providers.anthropic import AnthropicProvider
from services.ai.providers.google import GoogleProvider
from services.ai.providers.deepseek import DeepseekProvider

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Priority order: first entry with the required capability wins
# ---------------------------------------------------------------------------
_PROVIDER_PRIORITY = ["deepseek", "anthropic", "google"]


class NoProviderAvailableError(Exception):
    """Raised when no provider can satisfy the required capability."""


class AIProviderManager:
    """
    Resolves and caches AI providers for a single user session.

    Usage:
        manager = AIProviderManager.from_user_settings(session, user_uuid, master_key)
        provider = manager.get_provider(ModelCapability.TEXT | ModelCapability.VISION)
    """

    def __init__(self, providers: dict[str, AIProvider]):
        """
        providers: mapping of provider name → AIProvider instance.
        Only providers with a valid API key are included.
        """
        self._providers = providers

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_user_settings(
        cls,
        session: Any,
        user_uuid: str,
        master_key: bytes | str,
    ) -> "AIProviderManager":
        """
        Build a manager from the user's persisted settings.
        Only providers for which the user has a valid, decrypted API key
        will be instantiated.
        """
        from services.settings import get_or_create_settings
        from services.encryption import decrypt_data

        master_key_str = (
            master_key.decode() if isinstance(master_key, bytes) else master_key
        )
        user_settings = get_or_create_settings(session, user_uuid, master_key_str)

        if not user_settings.ai_feature_enabled:
            return cls({})

        def _decrypt(enc_value: str | None) -> str | None:
            if not enc_value:
                return None
            try:
                decrypted = decrypt_data(enc_value, master_key_str)
                return None if decrypted.startswith("Error:") else decrypted
            except Exception:
                return None

        providers: dict[str, AIProvider] = {}

        claude_key = _decrypt(getattr(user_settings, "claude_api_key_enc", None))
        if claude_key:
            try:
                providers["anthropic"] = AnthropicProvider(api_key=claude_key)
            except Exception as exc:
                logger.warning("Failed to init AnthropicProvider: %s", exc)

        gemini_key = _decrypt(getattr(user_settings, "gemini_api_key_enc", None))
        if gemini_key:
            try:
                providers["google"] = GoogleProvider(api_key=gemini_key)
            except Exception as exc:
                logger.warning("Failed to init GoogleProvider: %s", exc)

        deepseek_key = _decrypt(getattr(user_settings, "deepseek_api_key_enc", None))
        if deepseek_key:
            try:
                providers["deepseek"] = DeepseekProvider(api_key=deepseek_key)
            except Exception as exc:
                logger.warning("Failed to init DeepseekProvider: %s", exc)

        return cls(providers)

    # ------------------------------------------------------------------
    # Provider resolution
    # ------------------------------------------------------------------

    def get_provider(
        self,
        required: ModelCapability = ModelCapability.TEXT,
        preferred: str | None = None,
    ) -> AIProvider:
        """
        Return the best available provider that satisfies `required`.

        Args:
            required:  Capability flags the provider must support.
            preferred: Optional provider name ("anthropic" | "google" | "deepseek")
                       to try first before falling back to priority order.

        Raises:
            NoProviderAvailableError: if no configured provider meets the requirement.
        """
        candidates: list[str] = list(_PROVIDER_PRIORITY)

        # Put preferred provider at the front if supplied and available
        if preferred and preferred in candidates:
            candidates.remove(preferred)
            candidates.insert(0, preferred)

        for name in candidates:
            provider = self._providers.get(name)
            if provider and provider.supports(required):
                logger.debug("Selected provider '%s' for capability %s", name, required)
                return provider

        raise NoProviderAvailableError(
            f"No AI provider available for capability: {required}. "
            f"Configured providers: {list(self._providers.keys())}"
        )

    def available_providers(self) -> dict[str, ModelCapability]:
        """Return a map of configured provider name → its capabilities."""
        return {name: p.capabilities() for name, p in self._providers.items()}

    def has_any_provider(self) -> bool:
        return bool(self._providers)
