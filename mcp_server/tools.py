"""The tools an MCP client can call against a CapitalView account.

Read-only by design. Every one of them resolves the caller from the request
context, opens its own short-lived database session, and hands the account's
Master Key down to the service layer — the same key path the web app uses, so a
tool can never see more than the user themselves can.

The set is deliberately small. Four tools that answer the questions people
actually ask about their money beat twenty that mirror the REST surface: an
agent picks better from a short menu, and each extra tool costs context on every
single request.
"""

from typing import Any

from pydantic_core import to_jsonable_python

from mcp_server.context import require_scope
from mcp_server.db import open_session
from services.ai.tools import (
    get_historical_performance,
    get_user_balance,
    get_user_cashflow,
)
from services.analytics.report import build_investor_analytics
from services.api_token import READ_SCOPE


def _jsonable(payload: Any) -> Any:
    """Flatten Decimals, dates and pydantic models into JSON-safe values."""
    return to_jsonable_python(payload)


def register_tools(mcp) -> None:
    """Attach every CapitalView tool to *mcp*."""

    @mcp.tool(
        title="Vue d'ensemble du patrimoine",
        description=(
            "Répartition du patrimoine de l'utilisateur : actions, crypto, liquidités "
            "bancaires, autres actifs, et valeur totale en euros. À appeler en premier "
            "pour situer la conversation. `details` ajoute le détail compte par compte "
            "et position par position. `date` (YYYY-MM-DD) donne l'état du patrimoine "
            "à une date passée au lieu d'aujourd'hui."
        ),
    )
    def get_portfolio_overview(details: bool = False, date: str | None = None) -> dict:
        principal = require_scope(READ_SCOPE)
        with open_session() as session:
            return _jsonable(
                get_user_balance(
                    session,
                    principal.user_uuid,
                    principal.master_key,
                    details=details,
                    date=date,
                )
            )

    @mcp.tool(
        title="Performance historique",
        description=(
            "Évolution et plus-values latentes (PnL) des investissements sur les "
            "`days` derniers jours. Pour analyser une tendance dans le temps — pour la "
            "photo du patrimoine à l'instant T, utiliser get_portfolio_overview. "
            "`account_type` vaut 'stock', 'crypto' ou 'all'."
        ),
    )
    def get_performance(days: int = 30, account_type: str = "all") -> dict:
        principal = require_scope(READ_SCOPE)
        with open_session() as session:
            return _jsonable(
                get_historical_performance(
                    session,
                    principal.user_uuid,
                    principal.master_key,
                    days=days,
                    account_type=account_type,
                )
            )

    @mcp.tool(
        title="Budget et flux",
        description=(
            "Revenus, dépenses, solde et taux d'épargne de l'utilisateur. "
            "`flow_type` ('inflow' ou 'outflow') restreint à un sens ; laisser vide "
            "pour la vue globale. `details` renvoie le détail brut des flux."
        ),
    )
    def get_cashflow_summary(details: bool = False, flow_type: str | None = None) -> dict:
        principal = require_scope(READ_SCOPE)
        with open_session() as session:
            return _jsonable(
                get_user_cashflow(
                    session,
                    principal.user_uuid,
                    principal.master_key,
                    details=details,
                    flow_type=flow_type,
                )
            )

    @mcp.tool(
        title="Analyse du comportement d'investisseur",
        description=(
            "Rapport d'analyse comportementale sur tout l'historique boursier : écart "
            "à l'indice de référence, respect du plan d'investissement déclaré, frais "
            "réels et estimés, régularité des versements. Réponse volumineuse — à "
            "appeler quand l'utilisateur veut un diagnostic de fond, pas pour un chiffre."
        ),
    )
    def get_investor_analytics() -> dict:
        principal = require_scope(READ_SCOPE)
        with open_session() as session:
            return _jsonable(
                build_investor_analytics(session, principal.user_uuid, principal.master_key)
            )
