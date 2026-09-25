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


def recommend(provider_id: str, models: list[DetectedModel]) -> str | None:
    """Return the model "automatic" stands for among `models`, None if there are none."""
    entry = PROVIDER_REGISTRY.get(provider_id)
    if entry is None or not models:
        return None
    # One model serves every use of a provider, so one that offers the photo
    # import keeps to the models that read images, as long as there is one.
    candidates = models
    if "vision" in entry["capabilities"]:
        candidates = [m for m in models if m.vision] or models
    for pattern in entry["preferred"]:
        family = [m for m in candidates if re.search(pattern, m.id)]
        if family:
            return max(family, key=lambda m: _version(m.id)).id
    return candidates[0].id


async def settle_model(provider: AIProvider, required: ModelCapability) -> bool:
    """Fix the model the provider will call, and say whether it covers `required`.

    A provider on "automatic" gets the recommended model. A model known not to
    read images turns the provider down for the photo import, so the next one
    in line gets its chance.
    """
    if not provider.supports(required):
        return False
    try:
        models = await detect_models(provider)
    except Exception as exc:
        # The call itself will say what is wrong (a refused key, a network
        # failure) far better than a listing that failed the same way.
        logger.warning("could not list %s models: %r", provider.provider_id, exc)
        models = []
    if provider.model is None:
        provider.model = recommend(provider.provider_id, models) or get_fallback_model(
            provider.provider_id
        )
    if ModelCapability.VISION in required:
        chosen = next((m for m in models if m.id == provider.model), None)
        if chosen is not None and not chosen.vision:
            return False
    return True
