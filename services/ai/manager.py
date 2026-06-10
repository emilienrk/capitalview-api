"""
AI Provider Manager.

Resolves the best available AI provider for a given user and required capability.

Resolution order (per capability):
    1. Respect user's explicit preference (ai_vision_provider / ai_chat_provider)
    2. Fallback to CAPABILITY_PRIORITY order
    3. Skip providers without a valid API key

The manager reads provider configs from the `user_ai_providers` table and
instantiates providers with the user's preferred model (or the registry default).
"""

from __future__ import annotations

import logging
from typing import Any

from services.ai.providers.base import AIProvider, ModelCapability
from services.ai.providers.anthropic import AnthropicProvider
from services.ai.providers.google import GoogleProvider
from services.ai.providers.deepseek import DeepseekProvider
from services.ai.registry import (
    PROVIDER_REGISTRY,
    CAPABILITY_PRIORITY,
    get_default_model,
    provider_supports,
)

logger = logging.getLogger(__name__)

# Map capability string → ModelCapability flag(s)
_CAPABILITY_FLAGS: dict[str, ModelCapability] = {
    "vision": ModelCapability.TEXT | ModelCapability.VISION,
    "chat":   ModelCapability.TEXT,
}


class NoProviderAvailableError(Exception):
    """Raised when no provider can satisfy the required capability."""


class AIProviderManager:
    """
    Resolves and caches AI providers for a single user session.

    Usage:
        manager = AIProviderManager.from_user_settings(session, user_uuid, master_key)
        provider = manager.get_provider_for_capability("vision")
        provider = manager.get_provider(ModelCapability.TEXT | ModelCapability.VISION)
    """

    def __init__(
        self,
        providers: dict[str, AIProvider],
        vision_preference: str | None = None,
        chat_preference: str | None = None,
    ):
        """
        providers: mapping of provider name → AIProvider instance.
        Only providers with a valid API key are included.
        """
        self._providers = providers
        self._preferences: dict[str, str | None] = {
            "vision": vision_preference,
            "chat":   chat_preference,
        }

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
        from sqlmodel import select
        from models import UserSettings, UserAIProvider
        from services.settings import get_or_create_settings
        from services.encryption import decrypt_data, hash_index

        master_key_str = (
            master_key.decode() if isinstance(master_key, bytes) else master_key
        )
        user_settings = get_or_create_settings(session, user_uuid, master_key_str)

        if not user_settings.ai_feature_enabled:
            return cls({})

        user_bidx = hash_index(user_uuid, master_key_str)

        # Load all provider rows for this user
        provider_rows = list(
            session.exec(
                select(UserAIProvider).where(UserAIProvider.user_uuid_bidx == user_bidx)
            ).all()
        )

        def _decrypt(enc_value: str | None) -> str | None:
            if not enc_value:
                return None
            try:
                decrypted = decrypt_data(enc_value, master_key_str)
                return None if decrypted.startswith("Error:") else decrypted
            except Exception:
                return None

        providers: dict[str, AIProvider] = {}

        for row in provider_rows:
            provider_id = row.provider
            api_key = _decrypt(row.api_key_enc)
            if not api_key:
                continue

            # Resolve the model: user preference > registry default
            model = row.selected_model or get_default_model(provider_id)

            try:
                if provider_id == "anthropic":
                    kwargs = {"api_key": api_key}
                    if model:
                        kwargs["model"] = model
                    providers["anthropic"] = AnthropicProvider(**kwargs)

                elif provider_id == "google":
                    kwargs = {"api_key": api_key}
                    if model:
                        kwargs["model"] = model
                    providers["google"] = GoogleProvider(**kwargs)

                elif provider_id == "deepseek":
                    kwargs = {"api_key": api_key}
                    if model:
                        kwargs["model"] = model
                    providers["deepseek"] = DeepseekProvider(**kwargs)

                else:
                    logger.warning("Unknown provider '%s' — skipping.", provider_id)

            except Exception as exc:
                logger.warning("Failed to init provider '%s': %s", provider_id, exc)

        return cls(
            providers=providers,
            vision_preference=user_settings.ai_vision_provider,
            chat_preference=user_settings.ai_chat_provider,
        )

    # ------------------------------------------------------------------
    # Provider resolution
    # ------------------------------------------------------------------

    def get_provider_for_capability(self, capability: str) -> AIProvider:
        """
        Return the best available provider for the given capability string.

        Resolution order:
          1. User's explicit preference for this capability (if configured & available)
          2. CAPABILITY_PRIORITY order from the registry

        Args:
            capability: "vision" | "chat"

        Raises:
            NoProviderAvailableError
        """
        priority = list(CAPABILITY_PRIORITY.get(capability, []))

        # Put user's preference first
        preferred = self._preferences.get(capability)
        if preferred and preferred in priority:
            priority.remove(preferred)
            priority.insert(0, preferred)

        required_flags = _CAPABILITY_FLAGS.get(capability, ModelCapability.TEXT)

        for name in priority:
            provider = self._providers.get(name)
            if provider and provider.supports(required_flags):
                logger.debug(
                    "Selected provider '%s' for capability '%s'", name, capability
                )
                return provider

        raise NoProviderAvailableError(
            f"No AI provider available for capability: '{capability}'. "
            f"Configured providers: {list(self._providers.keys())}"
        )

    def get_provider(
        self,
        required: ModelCapability = ModelCapability.TEXT,
        preferred: str | None = None,
    ) -> AIProvider:
        """
        Return the best available provider that satisfies `required` capability flags.

        Args:
            required:  Capability flags the provider must support.
            preferred: Optional provider name to try first.

        Raises:
            NoProviderAvailableError
        """
        # Build candidate list from all configured providers
        all_names = list(self._providers.keys())

        if preferred and preferred in all_names:
            all_names.remove(preferred)
            all_names.insert(0, preferred)

        for name in all_names:
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

    def has_vision_provider(self) -> bool:
        """Return True if at least one configured provider supports vision."""
        required = ModelCapability.TEXT | ModelCapability.VISION
        return any(p.supports(required) for p in self._providers.values())
