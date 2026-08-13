"""The in-app assistant's tool surface: Anthropic-format schemas and registry.

What the agent can call and how the schemas are worded lives here. What the
calls actually compute lives in ``services/overview.py`` — those read models
are shared with the MCP server and the dashboard, so they are not this module's
to reshape.
"""

from typing import Any, Callable

from services.overview import (
    get_historical_performance,
    get_performance_since_last_login,
    get_user_balance,
    get_user_cashflow,
)


def get_tools() -> list:
    return [
        {
            "name": "get_user_balance",
            "description": "Obtenir la repartition de la valeur du patrimoine de l'utilisateur (actions, crypto, cash, valeur totale)."
            "Il doit etre utilise avant tout autre outil pour que l'agent puisse se faire une idee de la situation globale de l'utilisateur. "
            "Possibilite de demander les details des comptes et positions pour une vue plus granulaire.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "details": {
                        "type": "boolean",
                        "description": "Boolean pour inclure le detail des comptes et positions. (default: false)",
                        "default": False
                    },
                    "date": {
                        "type": "string",
                        "description": "Optionnel. La date cible sous format 'YYYY-MM-DD' uniquement si l'utilisateur demande explicitement un etat de son patrimoine a une date passee.",
                    }
                }
            }
        },
        {
            "name": "get_historical_performance",
            "description": "Obtenir la performance dynamique et les plus-values (PnL) des investissements de l'utilisateur sur une periode donnee pour observer son evolution temporelle."
            "L'agent peut utiliser cet outil pour analyser la tendance de l'utilisateur et observer les differences sur un laps de temps."
            "Ce tool est a utiliser EXCLUSIVEMENT pour une analyse de l'evolution et de sa performance historique, pas pour la photo a un instant T du patrimoine (utiliser get_user_balance).",
            "input_schema": {
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "Le nombre de jours de l'historique de perfromance a recuperer (ex: 7 pour la derniere semaine, 30 pour le dernier mois).",
                        "default": 10
                    },
                    "account_type": {
                        "type": "string",
                        "description": "Le type de compte pour lequel recuperer la performance (ex: 'stock' ou 'crypto' ou 'all').",
                        "default": "stock"
                    }
                }
            }
        },
        {
            "name": "get_user_cashflow",
            "description": "Obtenir les statistiques liees au budget (revenus/inflows, depenses/outflows, epargne/savings) de l'utilisateur.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "details": {
                        "type": "boolean",
                        "description": "Boolean pour inclure le detail brut des cashflows.",
                        "default": False
                    },
                    "flow_type": {
                        "type": "string",
                        "description": "Permet de filtrer specifiquement pour un type de transaction (ex: 'inflow' ou 'outflow'). Laisser vide pour avoir la vue globale.",
                        "enum": ["inflow", "outflow"]
                    }
                }
            }
        },
        {
            "name": "get_performance_since_last_login",
            "description": "Recupere la performance du patrimoine depuis la derniere connexion de l'utilisateur, en calculant la variation sur ses comptes actions et cryptos.",
            "input_schema": {
                "type": "object",
                "properties": {}
            }
        }
    ]


def get_tool_registry() -> dict[str, Callable[..., Any]]:
    return {
        "get_user_balance": get_user_balance,
        "get_historical_performance": get_historical_performance,
        "get_user_cashflow": get_user_cashflow,
        "get_performance_since_last_login": get_performance_since_last_login,
    }
