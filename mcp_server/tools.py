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

from decimal import Decimal
from typing import Any

from pydantic_core import to_jsonable_python

from mcp_server.context import require_scope
from mcp_server.db import open_session
from services.analytics.report import build_investor_analytics
from services.api_token import READ_SCOPE
from services.overview import (
    get_historical_performance,
    get_user_balance,
    get_user_cashflow,
)


def _jsonable(payload: Any) -> Any:
    """Flatten a tool's return value into JSON-safe values.

    Money must reach the model as a number from every tool. The read models in
    ``services/overview`` already cast to float, but the analytics report keeps
    Decimal — and the default serialiser turns a Decimal into a *string* to
    protect precision. Left alone, the same euro would arrive as ``12500.0``
    from one tool and ``"12500.55"`` from another, and a model comparing the two
    across a conversation has no reason to suspect the difference.

    Precision is not the concern it would be in a ledger: these figures are read
    and reasoned about, never written back, and float64 holds euro amounts far
    past any balance this application tracks.
    """
    return to_jsonable_python(_floats(payload))


def _floats(value: Any) -> Any:
    """Recursively replace Decimal with float, leaving everything else intact.

    Runs before serialisation rather than after, because once a Decimal has been
    rendered as a string it is indistinguishable from a genuine one.
    """
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {key: _floats(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_floats(item) for item in value]
    return value


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
