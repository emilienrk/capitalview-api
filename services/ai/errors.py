"""
Turn a failed AI call into an answer the user can act on.

A provider SDK error left uncaught ends in Starlette's ServerErrorMiddleware,
whose 500 carries no CORS header: the browser then reports a CORS failure and
the frontend a bare "Failed to fetch", while the actual cause (a refused key,
an unknown model, an exhausted quota) stays in the server logs.
"""

import logging
from urllib.parse import urlparse

import anthropic
import openai
from fastapi import Request
from fastapi.responses import JSONResponse
from google.genai import errors as genai_errors

from services.ai.manager import NoProviderAvailableError
from services.ai.providers.openrouter import OPENROUTER_BASE_URL

logger = logging.getLogger(__name__)

# The SDK behind each provider, named as the settings page names it
PROVIDER_ERRORS: dict[type[Exception], str] = {
    anthropic.APIError: "Claude (Anthropic)",
    genai_errors.APIError: "Gemini (Google)",
    openai.APIError: "DeepSeek",
}

# OpenRouter answers through the openai SDK too: the host the request went to
# tells it apart from DeepSeek.
_OPENROUTER_HOST = urlparse(OPENROUTER_BASE_URL).hostname

_MAX_PROVIDER_MESSAGE = 300


def _status_of(exc: Exception) -> int | None:
    # Anthropic and OpenAI expose `status_code`, google-genai `code`; a
    # connection error or a timeout carries neither.
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    return status if isinstance(status, int) else None


def _message_of(exc: Exception) -> str:
    message = getattr(exc, "message", None) or str(exc)
    message = " ".join(str(message).split())
    if len(message) > _MAX_PROVIDER_MESSAGE:
        message = message[:_MAX_PROVIDER_MESSAGE] + "…"
    return message


def _provider_of(exc: Exception) -> str:
    request = getattr(exc, "request", None)
    if isinstance(exc, openai.APIError) and request is not None:
        if request.url.host == _OPENROUTER_HOST:
            return "OpenRouter"
    return next(
        (name for cls, name in PROVIDER_ERRORS.items() if isinstance(exc, cls)),
        "IA",
    )


def describe_provider_error(exc: Exception) -> str:
    provider = _provider_of(exc)
    status = _status_of(exc)

    if status in (401, 403):
        advice = "clé API refusée. Vérifiez-la dans les paramètres."
    elif status == 404:
        advice = "modèle introuvable. Choisissez-en un autre dans les paramètres."
    elif status == 429:
        advice = "quota atteint ou trop de requêtes. Réessayez plus tard."
    elif status is None or status >= 500:
        advice = "service indisponible pour le moment. Réessayez plus tard."
    else:
        advice = "requête refusée."

    return f"Fournisseur {provider} : {advice} ({status or 'sans réponse'} : {_message_of(exc)})"


async def provider_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.warning("AI provider call failed on %s %s: %r", request.method, request.url.path, exc)
    # 503 rather than 502: Cloudflare swaps an origin 502 for its own error
    # page, which drops both the CORS header and the detail.
    return JSONResponse(status_code=503, content={"detail": describe_provider_error(exc)})


async def no_provider_handler(request: Request, exc: NoProviderAvailableError) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={
            "detail": (
                "Aucun fournisseur IA compatible n'est configuré. Ajoutez une clé API "
                "dans les paramètres (Gemini, Claude ou OpenRouter pour l'analyse de photos). "
                "Avec OpenRouter, choisissez un modèle qui lit les images."
            )
        },
    )


def register_ai_error_handlers(app) -> None:
    """Answer AI failures from inside the CORS middleware, with a readable detail."""
    app.add_exception_handler(NoProviderAvailableError, no_provider_handler)
    for error_class in PROVIDER_ERRORS:
        app.add_exception_handler(error_class, provider_error_handler)
