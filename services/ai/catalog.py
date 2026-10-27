"""
Detect the models a user's key can reach, and settle "automatic" on one.

A fixed model list goes stale: providers retire a model and every call on it
fails. The list is read from the provider instead, and a provider left on
"automatic" calls the best current match for the registry's preferences.
"""

import logging
import re
import time

from services.ai.providers.base import AIProvider, DetectedModel, ModelCapability
from services.ai.registry import PROVIDER_REGISTRY, get_fallback_model

logger = logging.getLogger(__name__)

# Long enough that a burst of calls lists the models once, short enough that a
# new release shows up within the hour. Per worker, which only means a few
# more listings.
CACHE_TTL_SECONDS = 3600

_cache: dict[tuple[str, str], tuple[float, list[DetectedModel]]] = {}


async def detect_models(provider: AIProvider, *, fresh: bool = False) -> list[DetectedModel]:
    """Return the models the provider's key can reach, listed at most once an hour.

    `fresh` lists them again whatever the cache holds: the settings page asks
    for it, so what it shows is what the key reaches now.
    """
    key = (provider.provider_id, provider.key_fingerprint)
    cached = _cache.get(key)
    if cached is not None and not fresh and cached[0] > time.monotonic():
        return cached[1]
    models = await provider.list_models()
    _cache[key] = (time.monotonic() + CACHE_TTL_SECONDS, models)
    return models


def _version(model_id: str) -> tuple[tuple[int, ...], bool]:
    # What follows a preview tag or a date is a snapshot, not a version:
    # "gemini-2.5-flash-preview-09-2025" is a 2.5, "claude-sonnet-4-5-20250929" a 4.5.
    head = re.split(r"-(?:preview|exp|latest|\d{8})", model_id)[0]
    numbers = tuple(int(n) for n in re.findall(r"\d+", head))
    stable = not re.search(r"preview|exp", model_id)
    return numbers, stable


def recommend(
    provider_id: str, models: list[DetectedModel], *, vision: bool = False
) -> str | None:
    """Return the model "automatic" stands for among `models`, None if there are none.

    `vision` keeps to the models that read images, for the photo import: None
    then means the key reaches none.
    """
    entry = PROVIDER_REGISTRY.get(provider_id)
    if entry is None:
        return None
    candidates = [m for m in models if m.vision] if vision else models
    if not candidates:
        return None
    patterns = (entry["preferred_vision"] if vision else []) + entry["preferred"]
    for pattern in patterns:
        family = [m for m in candidates if re.search(pattern, m.id)]
        if family:
            return max(family, key=lambda m: _version(m.id)).id
    return candidates[0].id


async def settle_model(provider: AIProvider, required: ModelCapability) -> bool:
    """Fix the model the provider will call, and say whether it covers `required`.

    Each use has its own choice: the photo import calls the provider's vision
    model, the chat its chat model. One left on "automatic" gets the
    recommended model for that use. A model known not to read images turns the
    provider down for the photo import, so the next one in line gets its chance.
    """
    if not provider.supports(required):
        return False
    vision = ModelCapability.VISION in required
    try:
        models = await detect_models(provider)
    except Exception as exc:
        # The call itself will say what is wrong (a refused key, a network
        # failure) far better than a listing that failed the same way.
        logger.warning("could not list %s models: %r", provider.provider_id, exc)
        models = []
    chosen = provider.vision_model if vision else provider.chat_model
    if chosen is None:
        if models:
            chosen = recommend(provider.provider_id, models, vision=vision)
        else:
            chosen = get_fallback_model(provider.provider_id, vision=vision)
        if chosen is None:
            # A key that reaches no model reading images, or no list to tell
            return False
    provider.model = chosen
    if vision:
        known = next((m for m in models if m.id == chosen), None)
        if known is not None and not known.vision:
            return False
    return True
