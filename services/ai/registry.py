"""
Static registry of supported AI providers.

This is the single source of truth for:
- Which providers are supported
- What capabilities each provider offers
- Which models are available for each provider

Adding a new provider = add an entry here + create its provider class.
No database migration needed.
"""

from typing import TypedDict


class ModelEntry(TypedDict, total=False):
    id: str
    label: str
    default: bool  # True = used when no model is explicitly selected


class ProviderEntry(TypedDict):
    label: str
    capabilities: list[str]  # subset of ["vision", "chat"]
    models: list[ModelEntry]


PROVIDER_REGISTRY: dict[str, ProviderEntry] = {
    "google": {
        "label": "Gemini (Google)",
        "capabilities": ["vision", "chat"],
        "models": [
            {"id": "gemini-3.5-flash",     "label": "Gemini 3.5 Flash", "default": True},
            {"id": "gemini-3.1-pro",       "label": "Gemini 3.1 Pro"},
            {"id": "gemini-3.1-flash-lite","label": "Gemini 3.1 Flash Lite"},
            {"id": "gemini-2.5-flash",     "label": "Gemini 2.5 Flash"},
            {"id": "gemini-2.5-pro",       "label": "Gemini 2.5 Pro"},
        ],
    },
    "anthropic": {
        "label": "Claude (Anthropic)",
        "capabilities": ["vision", "chat"],
        "models": [
            {"id": "claude-sonnet-4-6", "label": "Claude Sonnet 4.6", "default": True},
            {"id": "claude-opus-4-8",   "label": "Claude Opus 4.8"},
            {"id": "claude-haiku-4-5",  "label": "Claude Haiku 4.5"},
        ],
    },
    "deepseek": {
        "label": "DeepSeek",
        "capabilities": ["chat"],
        "models": [
            {"id": "deepseek-v4-flash", "label": "DeepSeek V4 Flash", "default": True},
            {"id": "deepseek-v4-pro",   "label": "DeepSeek V4 Pro"},
        ],
    },
}

# Default provider priority per capability (used when user has no explicit preference)
CAPABILITY_PRIORITY: dict[str, list[str]] = {
    "vision": ["google", "anthropic"],
    "chat":   ["google", "deepseek", "anthropic"],
}


def get_default_model(provider: str) -> str | None:
    """Return the default model ID for a provider, or None if not found."""
    entry = PROVIDER_REGISTRY.get(provider)
    if not entry:
        return None
    for model in entry["models"]:
        if model.get("default"):
            return model["id"]
    models = entry["models"]
    return models[0]["id"] if models else None


def provider_supports(provider: str, capability: str) -> bool:
    """Return True if the provider supports the given capability."""
    entry = PROVIDER_REGISTRY.get(provider)
    return bool(entry and capability in entry["capabilities"])


def providers_for_capability(capability: str) -> list[str]:
    """Return list of provider names that support the given capability, in priority order."""
    priority = CAPABILITY_PRIORITY.get(capability, [])
    return [p for p in priority if provider_supports(p, capability)]
