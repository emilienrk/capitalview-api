"""Per-request principal for MCP tool calls.

The MCP protocol has no place to carry "who is asking" into a tool body: a tool
receives only its declared arguments. The transport does know, though — it read
the bearer token off the HTTP request — so the authentication middleware parks
the resolved principal in a ContextVar that the tools read back.

A ContextVar is the right shape here precisely because the server is stateless:
the value lives for one request and is never shared between them, so two
concurrent MCP calls from different accounts cannot see each other's data.
"""

from contextvars import ContextVar

from services.api_token import ApiTokenPrincipal

_principal: ContextVar[ApiTokenPrincipal | None] = ContextVar("mcp_principal", default=None)


class McpAuthError(Exception):
    """Raised inside a tool when the request carries no usable principal."""


def set_principal(principal: ApiTokenPrincipal | None) -> None:
    """Bind the caller for the current request context."""
    _principal.set(principal)


def get_principal() -> ApiTokenPrincipal:
    """
    Return the authenticated caller.

    Raises:
        McpAuthError: if called outside an authenticated request. The middleware
            rejects anonymous requests before they reach a tool, so this only
            fires if a tool is invoked outside the HTTP path.
    """
    principal = _principal.get()
    if principal is None:
        raise McpAuthError("Requête MCP non authentifiée.")
    return principal


def require_scope(scope: str) -> ApiTokenPrincipal:
    """
    Return the caller, refusing them if the token lacks *scope*.

    Every tool goes through here rather than trusting the middleware alone, so
    adding a write tool later cannot accidentally inherit read-only clearance.
    """
    principal = get_principal()
    if not principal.has_scope(scope):
        raise McpAuthError(f"Ce token n'a pas la permission « {scope} ».")
    return principal
