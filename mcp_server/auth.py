"""Bearer authentication for the MCP endpoint.

Deliberately not the SDK's ``token_verifier`` hook: that path only installs its
authentication middleware when ``AuthSettings`` is also supplied, and those
settings mandate an OAuth issuer and resource-server metadata URL. CapitalView
issues personal access tokens, not OAuth grants, so advertising an authorization
server we do not run would be a lie clients could act on. A plain ASGI
middleware gates the endpoint with the same rigour and none of the pretence.
"""

import json
import logging

from mcp_server.context import set_principal
from mcp_server.db import open_session
from services.api_token import authenticate_api_token

logger = logging.getLogger(__name__)

_UNAUTHORIZED_BODY = json.dumps(
    {
        "error": "unauthorized",
        "error_description": "Token API CapitalView manquant ou invalide.",
    }
).encode("utf-8")


class ApiTokenAuthMiddleware:
    """Resolve ``Authorization: Bearer`` into a principal, or reject with 401."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        token = _bearer_token(scope)
        principal = None

        if token:
            try:
                with open_session() as session:
                    principal = authenticate_api_token(session, token)
            except Exception:
                # A database hiccup must read as "not authenticated", never as
                # "authenticated": failing open here would expose every account.
                logger.exception("MCP token authentication failed")
                principal = None

        if principal is None:
            return await _reject(send)

        set_principal(principal)
        try:
            await self.app(scope, receive, send)
        finally:
            set_principal(None)


def _bearer_token(scope) -> str | None:
    """Pull the bearer credential out of the raw ASGI headers."""
    for raw_name, raw_value in scope.get("headers", []):
        if raw_name.lower() != b"authorization":
            continue
        try:
            value = raw_value.decode("latin-1")
        except UnicodeDecodeError:
            return None
        scheme, _, credential = value.partition(" ")
        if scheme.lower() != "bearer":
            return None
        return credential.strip() or None
    return None


async def _reject(send) -> None:
    """Answer 401 with the challenge, without leaking why the token failed."""
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"www-authenticate", b'Bearer realm="CapitalView MCP"'),
                (b"content-length", str(len(_UNAUTHORIZED_BODY)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": _UNAUTHORIZED_BODY})
