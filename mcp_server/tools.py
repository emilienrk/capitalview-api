"""The tools an MCP client can call against a CapitalView account.

Read-only by design. Every one of them resolves the caller from the request
context, opens its own short-lived database session, and hands the account's
Master Key down to the service layer — the same key path the web app uses, so a
tool can never see more than the user themselves can.

The set is deliberately small. Seven tools that answer the questions people
actually ask about their money beat twenty that mirror the REST surface: an
agent picks better from a short menu, and each extra tool costs context on every
single request.

Three of them return sequences, and all are capped rather than trusted to be
small: a daily curve over several years, a full ledger, or a fifty-year
projection would spend the conversation's budget on one call. The caps live
here, in the layer that knows about context windows, not in the read models —
the web app charts the same curves at full resolution and must keep every point.

**Where a tool is allowed to read from.** Only neutral service modules — never
another consumer's module. A tool may call ``services/overview`` (cross-domain
read models) or ``services/analytics`` (its own subsystem, entered through
``report``) because both are owned by nobody and read by several callers as
peers. A tool may not reach into ``services/ai/`` or a ``routes/`` module: those
belong to the assistant and the web app, and shaping them around MCP's needs
would break their owner silently.

The two allowed sources look uneven — one small module, one 5 000-line
subsystem — but that is a difference of size, not of kind. Do not wrap the
analytics entry point in ``overview`` to make the imports look symmetrical: it
would be a function calling a function, and ``overview`` would end up having to
know every subsystem it forwards to.
"""

import datetime
from decimal import Decimal
from typing import Any

from pydantic_core import to_jsonable_python

from mcp_server.context import require_scope
from mcp_server.db import open_session
from services.analytics.report import build_investor_analytics
from services.api_token import READ_SCOPE
from services.overview import (
    build_projection,
    build_wealth_history,
    get_historical_performance,
    get_user_balance,
    get_user_cashflow,
    list_transactions,
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


# A conversation cannot afford an unbounded ledger or a multi-year daily series.
MAX_TRANSACTIONS = 200
MAX_HISTORY_POINTS = 120


def _as_date(value: str | None) -> datetime.date | None:
    """Parse a YYYY-MM-DD bound, refusing anything else rather than guessing."""
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Date attendue au format YYYY-MM-DD, reçu {value!r}.") from exc


ACCOUNT_TYPES = ("stock", "crypto", "all")
FLOW_TYPES = ("inflow", "outflow")


def _as_account_type(value: str) -> str:
    """Refuse an account type the read models do not know.

    They answer an unknown type with an empty result rather than an error, which
    a caller reading the code can see but a model cannot: ask for "bank" — the
    obvious guess, since the overview reports a cash total — and you get
    ``{"count": 0}``, which reads as "you never traded anything". A wrong answer
    about someone's money is worse than a refused one, so name the alternatives
    and let the model retry.
    """
    if value not in ACCOUNT_TYPES:
        raise ValueError(
            f"account_type doit valoir 'stock', 'crypto' ou 'all', reçu {value!r}. "
            "Les mouvements bancaires ne sont pas exposés ici."
        )
    return value


def _as_flow_type(value: str | None) -> str | None:
    """Same refusal for the cashflow direction, which silently ignores typos."""
    if value is not None and value.lower() not in FLOW_TYPES:
        raise ValueError(f"flow_type doit valoir 'inflow' ou 'outflow', reçu {value!r}.")
    return value


MAX_PROJECTION_MONTHS = 600


def _category_name(category: Any) -> str:
    """Name a category as "BANK", not "AccountCategory.BANK".

    ``str()`` on a str-Enum member yields the qualified form, which a model then
    repeats back to the user verbatim.
    """
    return getattr(category, "value", str(category))


def _as_months(months: int) -> int:
    """Clamp the projection horizon to something the service will accept."""
    return min(max(months, 1), MAX_PROJECTION_MONTHS)


def _projection_step(months: int) -> int:
    """Yearly milestones once the horizon is long enough to make months noise."""
    return 1 if months <= 36 else 12


def _projection_points(data: list, step: int) -> list[dict]:
    """Keep one point per step, always including the horizon itself.

    The final point is what the question was about — "where do I land" — so it
    survives whatever the step does to the rest of the curve.
    """
    kept = [point for index, point in enumerate(data, start=1) if index % step == 0]
    if data and (not kept or kept[-1] is not data[-1]):
        kept.append(data[-1])
    return [
        {
            "date": point.date,
            "total_value": point.total_value,
            "asset_values": {_category_name(key): value for key, value in point.asset_values.items()},
        }
        for point in kept[-MAX_HISTORY_POINTS:]
    ]


def _within_days(history: list[dict], days: int) -> list[dict]:
    """Keep the entries falling inside the last *days*, counted from the data.

    Counted from the newest snapshot rather than today: a portfolio whose
    history stops last month should still answer, instead of returning nothing
    because the window ends before the data starts.
    """
    if not history or days <= 0:
        return history
    cutoff = history[-1]["snapshot_date"] - datetime.timedelta(days=days)
    return [entry for entry in history if entry["snapshot_date"] > cutoff]


def _resolve_granularity(granularity: str, days: int) -> str:
    """Pick a step that keeps the series readable over the requested window."""
    if granularity in ("day", "week", "month"):
        return granularity
    if days <= 90:
        return "day"
    return "week" if days <= 730 else "month"


def _period_key(day: datetime.date, step: str) -> tuple:
    if step == "week":
        year, week, _ = day.isocalendar()
        return (year, week)
    if step == "month":
        return (day.year, day.month)
    return (day.year, day.month, day.day)


def _downsample(history: list[dict], step: str) -> list[dict]:
    """Keep the last entry of each period, then cap the number of points.

    Wealth is a level, not a flow: the closing value of a week describes it,
    where a sum would invent money and an average would smooth away the peak
    that made the period worth looking at.
    """
    if not history:
        return []

    by_period: dict[tuple, dict] = {}
    for entry in history:
        by_period[_period_key(entry["snapshot_date"], step)] = entry

    points = [by_period[key] for key in sorted(by_period)]
    return points[-MAX_HISTORY_POINTS:]


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
        # Validated here rather than left to the read model, which would parse it
        # deep inside and raise a Python format error at the model.
        day = _as_date(date)
        with open_session() as session:
            return _jsonable(
                get_user_balance(
                    session,
                    principal.user_uuid,
                    principal.master_key,
                    details=details,
                    date=day.isoformat() if day else None,
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
        kind = _as_account_type(account_type)
        with open_session() as session:
            return _jsonable(
                get_historical_performance(
                    session,
                    principal.user_uuid,
                    principal.master_key,
                    days=days,
                    account_type=kind,
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
        direction = _as_flow_type(flow_type)
        with open_session() as session:
            return _jsonable(
                get_user_cashflow(
                    session,
                    principal.user_uuid,
                    principal.master_key,
                    details=details,
                    flow_type=direction,
                )
            )

    @mcp.tool(
        title="Courbe du patrimoine",
        description=(
            "Évolution du patrimoine jour par jour sur les `days` derniers jours, "
            "ventilée entre actions, crypto, banque et autres actifs. C'est la série "
            "à utiliser pour décrire une trajectoire ou repérer un décrochage — "
            "get_performance ne donne que les bornes. `granularity` vaut 'auto' "
            "(défaut), 'day', 'week' ou 'month' ; 'auto' choisit le pas pour rester "
            "lisible sur la période demandée."
        ),
    )
    def get_wealth_history(days: int = 90, granularity: str = "auto") -> dict:
        principal = require_scope(READ_SCOPE)
        with open_session() as session:
            history = build_wealth_history(session, principal.user_uuid, principal.master_key)

        window = _within_days(history, days)
        step = _resolve_granularity(granularity, days)
        points = _downsample(window, step)

        return _jsonable(
            {
                "granularity": step,
                "points_returned": len(points),
                "days_covered": len(window),
                "points": points,
            }
        )

    @mcp.tool(
        title="Transactions",
        description=(
            "Mouvements d'achat et de vente de l'utilisateur, du plus récent au plus "
            "ancien. Pour répondre à « qu'est-ce que j'ai acheté en mars » ou retracer "
            "l'entrée sur une ligne. `account_type` vaut 'stock', 'crypto' ou 'all'. "
            "`since` et `until` sont des dates 'YYYY-MM-DD' incluses. Préférer une "
            "fenêtre resserrée : la réponse est plafonnée à 200 mouvements."
        ),
    )
    def list_recent_transactions(
        account_type: str = "all",
        since: str | None = None,
        until: str | None = None,
        limit: int = 50,
    ) -> dict:
        principal = require_scope(READ_SCOPE)
        kind = _as_account_type(account_type)
        with open_session() as session:
            movements = list_transactions(
                session,
                principal.user_uuid,
                principal.master_key,
                account_type=kind,
                since=_as_date(since),
                until=_as_date(until),
                limit=min(max(limit, 1), MAX_TRANSACTIONS),
            )

        return _jsonable({"count": len(movements), "transactions": movements})

    @mcp.tool(
        title="Projection du patrimoine",
        description=(
            "Projette le patrimoine sur `months` mois. Pour répondre à « où j'en "
            "serai dans X années » et pour comparer des scénarios d'épargne. "
            "`monthly_stock`, `monthly_crypto` et `monthly_bank` fixent l'apport "
            "mensuel en euros de chaque poche ; laisser vide reprend le rythme "
            "déduit de l'historique du compte — soit le montant investi étalé "
            "depuis la première transaction, ce qui suppose que l'utilisateur "
            "verse encore au même rythme. La réponse répète les hypothèses "
            "retenues dans `assumptions` : les citer, ne jamais présenter la "
            "courbe comme une prévision."
        ),
    )
    def project_wealth(
        months: int = 120,
        monthly_stock: float | None = None,
        monthly_crypto: float | None = None,
        monthly_bank: float | None = None,
    ) -> dict:
        principal = require_scope(READ_SCOPE)
        horizon = _as_months(months)
        with open_session() as session:
            projection = build_projection(
                session,
                principal.user_uuid,
                principal.master_key,
                months=horizon,
                monthly_stock=monthly_stock,
                monthly_crypto=monthly_crypto,
                monthly_bank=monthly_bank,
            )

        # The service answers a losing trajectory with an empty curve. Left as
        # is, that reads as "no projection available" when it actually means
        # "this ends up below what you put in" — the one outcome the user most
        # needs told.
        step = _projection_step(horizon)
        points = _projection_points(projection.data, step)
        assumptions = projection.parameters_used

        return _jsonable(
            {
                "months": horizon,
                "step_months": step,
                "ends_below_contributions": not projection.data,
                "note": (
                    "À ces hypothèses, le patrimoine projeté finit sous la somme "
                    "des versements : la courbe n'est pas rendue."
                    if not projection.data
                    else None
                ),
                "assumptions": {
                    "months_to_project": assumptions.months_to_project,
                    "assets": {
                        _category_name(category): {
                            "monthly_injection": used.monthly_injection,
                            "annual_return_rate": used.return_rate,
                        }
                        for category, used in assumptions.assets.items()
                    },
                },
                "points": points,
            }
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
