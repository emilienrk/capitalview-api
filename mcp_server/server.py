"""The CapitalView MCP server, mounted inside the FastAPI application.

Stateless on purpose. The 2026-07-28 protocol revision dropped the ``initialize``
handshake and protocol-level sessions, so every request is self-describing: any
worker can answer any call, nothing needs sticky routing, and there is no session
state to lose on redeploy. That matches how the API is already deployed.
"""

import logging

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.routing import Route

from config import get_settings
from mcp_server.auth import ApiTokenAuthMiddleware
from mcp_server.tools import register_tools

logger = logging.getLogger(__name__)

INSTRUCTIONS = """\
CapitalView est l'application de gestion de patrimoine personnel de l'utilisateur.
Ces outils lisent ses comptes, en lecture seule : titres, crypto, banque, placements,
biens, budget, et l'analyse de son comportement d'investisseur.

Quel outil pour quelle question :
- Où en est mon patrimoine, sa répartition, mes plus-values : get_portfolio_overview,
  à appeler en premier pour situer la conversation.
- Combien je dépense, gagne, épargne ; mon taux d'épargne ; ce mois-ci : get_cashflow.
- Un paiement précis, un marchand, un libellé : list_bank_operations.
- Abonnements, loyer, salaire et autres récurrents : get_recurring.
- Ce qu'ont rapporté mes investissements, mon rendement : get_performance.
- La trajectoire passée du patrimoine : get_wealth_history ; son avenir : project_wealth.
- Mes achats et ventes de titres ou de crypto : list_investment_transactions.
- Le budget que l'utilisateur a prévu, pas le réel : get_declared_budget.
- Un diagnostic de fond sur sa façon d'investir : get_investor_analytics.

Conventions : montants en euros ; un champ `*_pct` est en pourcent (12.5 = 12,5 %) ;
`annual_return_*` et `growth_share` sont des ratios (0.05 = 5 %) ; dates ISO ; un tableau
arrive en `columns` + `rows`.

Ces chiffres sont les données financières réelles de l'utilisateur, déjà calculées par
l'app : les citer tels quels plutôt que les recalculer ou les additionner soi-même, ne
jamais les extrapoler, et dire quand une donnée manque plutôt que l'estimer. Relayer les
`caveats`, `notes` et `freshness` qui touchent la question posée.\
"""


def build_mcp_server() -> MCPServer:
    """Build the MCP server and register the CapitalView tools on it."""
    server = MCPServer(
        name="capitalview",
        title="CapitalView",
        instructions=INSTRUCTIONS,
        website_url=get_settings().mcp_public_url,
    )
    register_tools(server)
    return server


def _transport_security() -> TransportSecuritySettings:
    """
    Host allow-list for the MCP endpoint.

    The SDK turns on DNS-rebinding protection by default, which is aimed at MCP
    servers bound to localhost where a malicious web page could reach them
    through the user's browser. This one is a public HTTPS API behind a reverse
    proxy, so the proxy's own Host header would trip that check and answer 421.
    Set MCP_ALLOWED_HOSTS to re-enable it with the real hostnames.
    """
    allowed_hosts = get_settings().mcp_allowed_hosts
    if not allowed_hosts:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=[f"https://{host}" for host in allowed_hosts],
    )


def build_mcp_route(server: MCPServer) -> Route:
    """
    Return the authenticated MCP endpoint as a route for the host application.

    A route rather than a mount: mounting at ``/mcp`` strips the prefix, leaving
    an empty path that the inner router answers with a 307 to ``/mcp/``. Clients
    are configured with the bare URL, and a redirect ahead of authentication is
    a round trip every call would pay. Routing keeps the path intact, so the
    inner app's own ``/mcp`` route matches on the first hop.

    ``json_response=True`` keeps every answer a plain JSON body rather than an
    SSE stream: none of these tools stream partial results, and a buffering
    reverse proxy would sit on an SSE response until it completed anyway.
    """
    path = get_settings().mcp_path
    app = server.streamable_http_app(
        streamable_http_path=path,
        json_response=True,
        stateless_http=True,
        transport_security=_transport_security(),
        host="0.0.0.0",
    )
    return Route(path, endpoint=ApiTokenAuthMiddleware(app))
