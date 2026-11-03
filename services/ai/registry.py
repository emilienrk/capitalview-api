"""
Static registry of supported AI providers.

This is the single source of truth for:
- Which providers are supported
- What capabilities each provider offers
- How "automatic" picks a model among those the user's key can reach

Which models read images is not listed here: it is read from each provider's
live list, so a provider serves the photo import as soon as its key reaches a
model that reads images.

Adding a new provider = add an entry here + create its provider class.
No database migration needed.
"""

from typing import TypedDict


class ProviderEntry(TypedDict):
    label: str
    capabilities: list[str]  # subset of ["vision", "chat"]; vision still needs a model that reads images
    # What "automatic" picks among the models the user's key can reach: the
    # first pattern with a match wins, at its highest version. Matched against
    # the provider's live list, so a new release is picked up without a deploy.
    preferred: list[str]
    # Patterns tried first for the photo import, among the models that read
    # images; the general ones above follow.
    preferred_vision: list[str]
    # Called when that list cannot be fetched; the call then reports the cause.
    fallback_model: str
    # The same for the photo import; None when no model can be assumed to read
    # images, so the import moves on to the next provider.
    fallback_vision_model: str | None


PROVIDER_REGISTRY: dict[str, ProviderEntry] = {
    "google": {
        "label": "Gemini (Google)",
        "capabilities": ["vision", "chat"],
        "preferred": [
            r"^gemini-[\d.]+-flash$",
            r"^gemini-[\d.]+-pro$",
            r"^gemini-.*flash",
            r"^gemini-",
        ],
        "preferred_vision": [],
        "fallback_model": "gemini-3.5-flash",
        "fallback_vision_model": "gemini-3.5-flash",
    },
    "anthropic": {
        "label": "Claude (Anthropic)",
        "capabilities": ["vision", "chat"],
        "preferred": [r"^claude-sonnet-", r"^claude-haiku-", r"^claude-opus-", r"^claude-"],
        "preferred_vision": [],
        "fallback_model": "claude-sonnet-4-6",
        "fallback_vision_model": "claude-sonnet-4-6",
    },
    "deepseek": {
        "label": "DeepSeek",
        "capabilities": ["vision", "chat"],
        "preferred": [r"^deepseek-flash$", r"^deepseek-v[\d.]+-flash$", r"^deepseek-chat$", r"^deepseek-"],
        "preferred_vision": [r"^deepseek-flash$", r"^deepseek-v[\d.]+-flash"],
        "fallback_model": "deepseek-flash",
        "fallback_vision_model": "deepseek-flash",
    },
    "openrouter": {
        "label": "OpenRouter",
        "capabilities": ["vision", "chat"],
        "preferred": [
            r"^google/gemini-[\d.]+-flash$",
            r"^anthropic/claude-sonnet-",
            r"^openai/gpt-[\d.]+-mini$",
            r"^google/gemini-",
        ],
        # OpenRouter's own router: an id that exists whatever the catalogue holds
        "preferred_vision": [],
        "fallback_model": "openrouter/auto",
        "fallback_vision_model": "openrouter/auto",
    },
}

# Default provider priority per capability (used when user has no explicit preference)
CAPABILITY_PRIORITY: dict[str, list[str]] = {
    "vision": ["google", "anthropic", "deepseek", "openrouter"],
    "chat":   ["google", "deepseek", "anthropic", "openrouter"],
}


def get_fallback_model(provider: str, *, vision: bool = False) -> str | None:
    """Return the model called when the provider's list cannot be fetched."""
    entry = PROVIDER_REGISTRY.get(provider)
    if entry is None:
        return None
    return entry["fallback_vision_model"] if vision else entry["fallback_model"]


def provider_supports(provider: str, capability: str) -> bool:
    """Return True if the provider supports the given capability."""
    entry = PROVIDER_REGISTRY.get(provider)
    return bool(entry and capability in entry["capabilities"])


def providers_for_capability(capability: str) -> list[str]:
    """Return list of provider names that support the given capability, in priority order."""
    priority = CAPABILITY_PRIORITY.get(capability, [])
    return [p for p in priority if provider_supports(p, capability)]
