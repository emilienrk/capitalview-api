"""The tools an MCP client can call against a CapitalView account.

Read-only by design. Every one of them resolves the caller from the request
context, opens its own short-lived database session, and hands the account's
Master Key down to the service layer — the same key path the web app uses, so a
tool can never see more than the user themselves can.

The set is deliberately small. Ten tools that answer the questions people
actually ask about their money beat thirty that mirror the REST surface: an
agent picks better from a short menu, and each extra tool costs context on every
single request.

**The model reads, it does not compute.** Every figure a question needs — a
share, a median month, a savings rate, a gain net of deposits — is computed by
the services and handed over as is. A model that adds up forty rows to answer
"how much do I spend" gets it wrong often enough to matter.

**Every answer is compact JSON**, one line, no indentation: the SDK would
otherwise indent it, a third of every answer spent on spaces. Long sequences go
as ``columns`` + ``rows`` rather than one object per row, which repeats no key.
Sequences are capped rather than trusted to be small — a daily curve over
years, a full ledger or a fifty-year projection would spend the conversation's
budget on one call — and a capped answer says so. The caps live here, in the
layer that knows about context windows, not in the read models: the web app
charts the same curves at full resolution and must keep every point.

**Where a tool is allowed to read from.** Only neutral service modules — never
another consumer's module. A tool may call ``services/overview`` (cross-domain
read models) or ``services/analytics`` (its own subsystem) because both are
owned by nobody and read by several callers as peers. ``services/banking``'s
``flows``, ``real_cashflow`` and ``recurring`` qualify on the same test — the
banking routes and this module read them as peers, and neither owns them. A
tool may not reach into ``services/ai/`` or a ``routes/`` module: those belong
to the assistant and the web app, and shaping them around MCP's needs would
break their owner silently.

Do not wrap the analytics entry points in ``overview`` to make the imports look
symmetrical: it would be a function calling a function, and ``overview`` would
end up having to know every subsystem it forwards to.
"""

import datetime
import json
import re
import unicodedata
from decimal import Decimal
from typing import Annotated, Any, Literal

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field
from pydantic_core import to_jsonable_python

from dtos.banking import CashflowType, RecurringDirection, RecurringState, RecurringStatus
from mcp_server.context import require_scope
from mcp_server.db import open_session
from services.analytics.benchmark import benchmark_return, user_benchmark
from services.analytics.period import period_performance
from services.analytics.projection_basis import BasisWarning, describe
from services.analytics.report import build_investor_analytics
from services.analytics.yearly import yearly_performance
from services.api_token import READ_SCOPE
from services.bank import get_user_bank_accounts
from services.banking.flows import DEDUCTED, list_operations
from services.banking.real_cashflow import (
    PeriodNotCompletedError,
    completed_period,
    real_cashflow_current,
    real_cashflow_month,
    real_cashflow_recent,
    real_cashflow_year,
)
from services.banking.recurring import list_recurring
from services.overview import (
    build_projection,
    build_wealth_history,
    get_user_balance,
    get_user_cashflow,
    list_transactions,
)

# Every tool reads, none writes, and each answers the same twice in a row: a
# client can call them without asking the user first.
READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)

MAX_TRANSACTIONS = 200
MAX_OPERATIONS = 100
MAX_HISTORY_POINTS = 120
MAX_PROJECTION_MONTHS = 600

Day = Annotated[str | None, Field(description="Date 'YYYY-MM-DD'.")]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _floats(value: Any) -> Any:
    """Recursively replace Decimal with float, leaving everything else intact.

    Money must reach the model as a number from every tool; the default
    serialiser turns a Decimal into a *string* to protect precision, and the
    same euro would then arrive as ``12500.0`` from one tool and ``"12500.55"``
    from another. Runs before serialisation, because once a Decimal has been
    rendered as a string it is indistinguishable from a genuine one.
    Precision is not a concern here: these figures are read, never written back.
    """
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {key: _floats(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_floats(item) for item in value]
    return value


def _jsonable(payload: Any) -> Any:
    return to_jsonable_python(_floats(payload))


def _render(payload: Any) -> str:
    """The answer as one line of JSON, money as numbers."""
    return json.dumps(_jsonable(payload), ensure_ascii=False, separators=(",", ":"))


def _money(value: Decimal | float | None) -> float | None:
    return round(float(value), 2) if value is not None else None


def _pct(value: Decimal | float | None) -> float | None:
    """A figure the services already express in percent, rounded for reading."""
    return round(float(value), 1) if value is not None else None


def _euros(value: Decimal | float) -> str:
    """An amount inside a sentence, as a French reader writes it: 1 220,50 €."""
    return f"{float(value):,.2f} €".replace(",", "\u202f").replace(".", ",")


def _table(columns: list[str], rows: list[list]) -> dict:
    return {"columns": columns, "rows": rows}


def tool(mcp, *, title: str, description: str):
    """Register a read-only tool whose answer is rendered compact JSON text."""
    return mcp.tool(title=title, description=description, annotations=READ_ONLY, structured_output=False)


# ---------------------------------------------------------------------------
# Argument checks the schema cannot express
# ---------------------------------------------------------------------------


# Only a ToolError carries its message back to the model; the SDK reads anything
# else as a crash and answers "Error executing tool <name>" with nothing in it.
def _as_date(value: str | None) -> datetime.date | None:
    """Parse a YYYY-MM-DD bound, refusing anything else rather than guessing."""
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError as exc:
        raise ToolError(f"Date attendue au format YYYY-MM-DD, reçu {value!r}.") from exc


_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_YEAR = re.compile(r"^\d{4}$")


def _as_month(value: str) -> str:
    if not _MONTH.match(value):
        raise ToolError(f"Mois attendu au format YYYY-MM, reçu {value!r}.")
    return value


def _shift_month(period: str, months: int) -> str:
    year, month = (int(part) for part in period.split("-"))
    index = year * 12 + month - 1 + months
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def _last_months(count: int, today: datetime.date) -> list[str]:
    """The `count` months ending on the current one, oldest first."""
    current = f"{today:%Y-%m}"
    return [_shift_month(current, -offset) for offset in range(count - 1, -1, -1)]


def _category_name(category: Any) -> str:
    """Name a category as "BANK", not "AccountCategory.BANK".

    ``str()`` on a str-Enum member yields the qualified form, which a model then
    repeats back to the user verbatim.
    """
    return getattr(category, "value", str(category))


def _words(text: str | None) -> str:
    """Lowercased, accents off: "Prélèvement" is found by "prelevement"."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


# ---------------------------------------------------------------------------
# Wealth curve
# ---------------------------------------------------------------------------


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


_CURVE_COLUMNS = ["date", "total", "bank", "stocks", "crypto", "placements", "assets"]


def _curve_row(entry: dict) -> list:
    return [
        entry["snapshot_date"],
        _money(entry["total_wealth"]),
        _money(entry["bank_value"]),
        _money(entry["stock_value"]),
        _money(entry["crypto_value"]),
        _money(entry.get("placements_value", 0)),
        _money(entry["assets_value"]),
    ]


def _curve_summary(points: list[dict]) -> dict | None:
    """What a reader of the curve looks for first, so nobody scans the rows for it."""
    if not points:
        return None
    first, last = points[0], points[-1]
    low = min(points, key=lambda p: p["total_wealth"])
    high = max(points, key=lambda p: p["total_wealth"])
    change = last["total_wealth"] - first["total_wealth"]
    return {
        "start": _money(first["total_wealth"]),
        "end": _money(last["total_wealth"]),
        "change": _money(change),
        "change_pct": _pct(change / first["total_wealth"] * 100) if first["total_wealth"] > 0 else None,
        "low": {"date": low["snapshot_date"], "total": _money(low["total_wealth"])},
        "high": {"date": high["snapshot_date"], "total": _money(high["total_wealth"])},
    }


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------


def _assumption(used) -> dict:
    """Pair a figure the projection used with the measurement behind it.

    Both halves matter to a reader: the rate alone invites "your portfolio makes
    7.8% a year" stated as fact, while the provenance turns it into "measured
    over four years of history, which the last two barely support".

    The reservations are rendered here rather than passed as codes — a model
    needs the sentence, and it is the web app that writes its own wording.
    """
    assumption = {
        "monthly_injection": used.monthly_injection,
        "annual_return_rate": used.return_rate,
    }
    if used.basis is None:
        return assumption

    assumption["basis"] = {
        "contribution": used.basis.contribution,
        "contribution_months": used.basis.contribution_months,
        "contribution_total": used.basis.contribution_total,
        "return": used.basis.return_,
        "return_days": used.basis.return_days,
        "warnings": [
            describe(BasisWarning(code=warning.code, values=warning.values))
            for warning in used.basis.warnings
        ],
    }
    return assumption


def _as_months(months: int) -> int:
    """Clamp the projection horizon to something the service will accept."""
    return min(max(months, 1), MAX_PROJECTION_MONTHS)


def _projection_step(months: int) -> int:
    """Yearly milestones once the horizon is long enough to make months noise."""
    return 1 if months <= 36 else 12


def _split_value(point, month: int, monthly_contribution: float, starting_value: float) -> dict:
    """Break a projected value into where each euro of it came from.

    Three parts that sum to the total: what was already there, what was paid in
    since, and what the return added. Without the split, "143 000 € in ten
    years" hides whether the portfolio earned 38 000 or the user simply paid in
    60 000 — which is most of what the question was about.

    ``contributed`` is counted, not accumulated: the service adds the same
    injection every month, so after *month* months exactly that many landed.
    ``growth`` is then taken by difference, which keeps the three parts adding
    up to the total the curve actually reports, rounding included.
    """
    contributed = monthly_contribution * month
    return {
        "date": point.date,
        "total_value": point.total_value,
        "starting_value": starting_value,
        "contributed": round(contributed, 2),
        "growth": round(float(point.total_value) - starting_value - contributed, 2),
        "asset_values": {_category_name(key): value for key, value in point.asset_values.items()},
    }


def _projection_outcome(points: list[dict]) -> dict | None:
    """The horizon's answer in one block: paid in, earned, ended at.

    ``growth_share`` says how much of the final value the portfolio produced
    rather than the saver — the figure that separates "I saved a lot" from "it
    compounded". None when the curve is empty, since there is no outcome to
    report.
    """
    if not points:
        return None

    final = points[-1]
    total = float(final["total_value"])
    invested = final["starting_value"] + final["contributed"]

    return {
        "starting_value": final["starting_value"],
        "contributed": final["contributed"],
        "growth": final["growth"],
        "final_value": total,
        "total_invested": round(invested, 2),
        "growth_share": round(final["growth"] / total, 4) if total else None,
    }


def _projection_points(
    data: list, step: int, monthly_contribution: float = 0.0
) -> list[dict]:
    """Keep one point per step, always including the horizon itself.

    The final point is what the question was about — "where do I land" — so it
    survives whatever the step does to the rest of the curve. The month index is
    read before thinning, so a point still knows how many contributions it has
    seen once its neighbours are gone.
    """
    if not data:
        return []

    starting_value = float(data[0].total_value)
    numbered = [
        _split_value(point, month, monthly_contribution, starting_value)
        for month, point in enumerate(data)
    ]

    # Month zero always survives: it is today's value, the baseline every other
    # point is read against, and a curve starting eleven months out is unusable.
    kept = [entry for month, entry in enumerate(numbered) if month == 0 or month % step == 0]
    if kept[-1] is not numbered[-1]:
        kept.append(numbered[-1])
    return kept[-MAX_HISTORY_POINTS:]


# ---------------------------------------------------------------------------
# Real cashflow
# ---------------------------------------------------------------------------


def _cashflow_totals(totals) -> dict:
    """One set of cashflow figures, named for what they mean rather than how
    the web app's columns are called."""
    return {
        "income": _money(totals.income),
        "expenses": _money(totals.expenses),
        "recurring_expenses": _money(totals.recurring),
        "one_off_expenses": _money(totals.one_off),
        "left_after_expenses": _money(totals.cashflow),
        "saved": _money(totals.saving),
        "invested": _money(totals.investment),
        "remaining": _money(totals.net),
        "savings_rate_pct": _pct(totals.savings_rate),
        "placed_rate_pct": _pct(totals.placed_rate),
    }


_MONTH_COLUMNS = [
    "month", "income", "expenses", "saved", "invested", "left_after_expenses", "savings_rate_pct",
    "operations", "atypical",
]


def _month_rows(months) -> dict:
    return _table(_MONTH_COLUMNS, [
        [
            m.period, _money(m.income), _money(m.expenses), _money(m.saving), _money(m.investment),
            _money(m.cashflow), _pct(m.savings_rate), m.operation_count, m.atypical,
        ]
        for m in months
    ])


def _counterparts(counterparts) -> list[dict]:
    return [
        {"name": c.name, "amount": _money(c.amount), "share_pct": _pct(c.share), "operations": c.operation_count}
        for c in counterparts
    ]


def _expenses(expenses) -> list[dict]:
    return [
        {"date": e.operation_date, "label": e.label, "amount": _money(e.amount), "account": e.account_name}
        for e in expenses
    ]


def _caveats(open_questions: int, open_amount, coverage_gaps, other_currencies, stale=()) -> list[str]:
    """What could make the figures wrong, as sentences a model can relay."""
    caveats = []
    if open_questions:
        caveats.append(
            f"{open_questions} opération(s) ({_euros(open_amount)}) attendent encore un classement "
            "de l'utilisateur dans l'app : ces chiffres peuvent encore bouger."
        )
    for gap in coverage_gaps:
        caveats.append(
            f"Le compte « {gap.account_name} » n'est connu que du {gap.first_day} au "
            f"{gap.covered_until} : un virement vers lui hors de ces dates compte comme une dépense."
        )
    for other in other_currencies:
        caveats.append(
            f"Opérations en {other.currency} tenues hors des totaux, sans conversion : "
            f"{float(other.inflow):.2f} reçus, {float(other.outflow):.2f} dépensés."
        )
    if stale:
        caveats.append(f"Solde peut-être daté (non synchronisé depuis une semaine) : {', '.join(stale)}.")
    return caveats


def _safety_net(net) -> dict | None:
    if net is None:
        return None
    return {
        "available": _money(net.available),
        "of_which_savings": _money(net.savings),
        "median_monthly_expenses": _money(net.monthly_expenses),
        "months_covered": _pct(net.months),
        "months_covered_by_savings": _pct(net.savings_months),
    }


def _recent_cashflow(recent) -> dict:
    stale = recent.safety_net.stale_accounts if recent.safety_net else []
    return {
        "window": {
            "from": recent.first_period,
            "to": recent.last_period,
            "covered_months": recent.covered_months,
            "history_starts": recent.history_starts,
        },
        "monthly_median": _cashflow_totals(recent.monthly_median),
        "monthly_mean": _cashflow_totals(recent.monthly_mean),
        "totals": _cashflow_totals(recent.totals),
        "by_year": [
            {"year": y.year, "covered_months": y.covered_months, "monthly_median": _cashflow_totals(y.monthly_median)}
            for y in recent.years
        ],
        "months": _month_rows(recent.months),
        "recurring_monthly_cost": _money(recent.running_recurring),
        "recurring_monthly_income": _money(recent.running_recurring_income),
        "safety_net": _safety_net(recent.safety_net),
        "top_expenses": _expenses(recent.top_expenses),
        "top_destinations": _counterparts(recent.top_destinations),
        "top_sources": _counterparts(recent.top_sources),
        "caveats": _caveats(
            recent.open_questions, recent.open_amount, recent.coverage_gaps, recent.other_currencies, stale
        ),
    }


def _year_cashflow(year) -> dict:
    stale = year.safety_net.stale_accounts if year.safety_net else []
    return {
        "year": year.year,
        "covered_months": year.covered_months,
        "years_available": year.years_available,
        "monthly_median": _cashflow_totals(year.monthly_median),
        "monthly_mean": _cashflow_totals(year.monthly_mean),
        "totals": _cashflow_totals(year.totals),
        "previous_year_same_months": (
            _cashflow_totals(year.previous_year_to_date) if year.previous_year_to_date else None
        ),
        # The current year only: its totals plus a median month for each month left.
        "year_end_projection": _cashflow_totals(year.projection) if year.projection else None,
        "months": _month_rows(year.months),
        "recurring_monthly_cost": _money(year.running_recurring),
        "recurring_monthly_income": _money(year.running_recurring_income),
        "safety_net": _safety_net(year.safety_net),
        "top_expenses": _expenses(year.top_expenses),
        "top_destinations": _counterparts(year.top_destinations),
        "top_sources": _counterparts(year.top_sources),
        "caveats": _caveats(year.open_questions, year.open_amount, year.coverage_gaps, year.other_currencies, stale),
    }


def _month_cashflow(month) -> dict:
    return {
        "month": month.period,
        "operations": month.operation_count,
        "totals": _cashflow_totals(month.totals),
        "previous_month_with_data": month.previous_period,
        "next_month_with_data": month.next_period,
        "recurring_expenses": [{"name": r.name, "amount": _money(r.amount)} for r in month.recurring],
        "recurring_income": [{"name": r.name, "amount": _money(r.amount)} for r in month.recurring_income],
        "top_expenses": _expenses(month.top_expenses),
        "top_destinations": _counterparts(month.top_destinations),
        "top_sources": _counterparts(month.top_sources),
        "caveats": _caveats(month.open_questions, month.open_amount, month.coverage_gaps, month.other_currencies),
    }


def _current_cashflow(current) -> dict:
    return {
        "month": current.period,
        "day": current.day,
        "spent_so_far": _money(current.spent_to_date),
        "of_which_pending": _money(current.pending_to_date),
        "usual_by_this_day": _money(current.median_to_date),
        "usual_whole_month": _money(current.median_month),
        "projected_month": _money(current.projection),
        "upcoming_payments": [{"name": u.name, "date": u.date, "amount": _money(u.amount)} for u in current.upcoming],
        "upcoming_payments_total": _money(current.upcoming_amount),
        "upcoming_income": [
            {"name": u.name, "date": u.date, "amount": _money(u.amount)} for u in current.upcoming_income
        ],
        "upcoming_income_total": _money(current.upcoming_income_amount),
        "caveats": [
            f"{_euros(current.open_amount)} d'opérations attendent un classement : le mois peut encore bouger."
        ] if current.open_amount else [],
    }


# ---------------------------------------------------------------------------
# Bank operations and recurring
# ---------------------------------------------------------------------------


_OPERATION_COLUMNS = ["date", "label", "amount", "type", "account", "note"]


def _operation_note(op) -> str | None:
    """What the row's reader needs to not count it wrong."""
    notes = []
    if op.is_pending:
        notes.append("en attente")
    if op.transfer_status in DEDUCTED:
        if op.transfer_status.value in ("reversal", "refund"):
            notes.append("annulé par une opération inverse")
        else:
            notes.append(f"virement interne ↔ {op.transfer_account_name}")
    elif op.transfer_status is not None:
        notes.append(f"peut-être un virement interne ↔ {op.transfer_account_name} (à confirmer)")
    if op.recurring is not None:
        notes.append(f"récurrent : {op.recurring.name}")
    if op.currency != "EUR":
        notes.append(op.currency)
    return " ; ".join(notes) or None


def _counts(op) -> bool:
    """Whether the operation weighs in a total: booked, in euros, not cancelled out or internal."""
    return not op.is_pending and op.currency == "EUR" and op.transfer_status not in DEDUCTED


_RECURRING_COLUMNS = [
    "name", "nature", "amount", "cadence", "monthly", "last", "next", "status", "paid_last_12_months", "note",
]


def _recurring_note(item) -> str | None:
    notes = []
    if item.variable:
        notes.append("montant variable")
    if item.state is RecurringState.CANDIDATE:
        notes.append("détecté, pas encore confirmé par l'utilisateur")
    if item.price_changes:
        change = item.price_changes[-1]
        notes.append(f"prix passé de {_money(change.before)} à {_money(change.after)} le {change.date}")
    if item.currency != "EUR":
        notes.append(item.currency)
    return " ; ".join(notes) or None


def _recurring_side(response, include_inactive: bool) -> dict:
    counted = (RecurringState.AUTO, RecurringState.CONFIRMED)
    items = [
        item for item in response.items
        if include_inactive
        or (item.state in counted and item.status is not RecurringStatus.ENDED)
    ]
    return {
        "monthly_total": _money(response.monthly_total),
        "annual_total": _money(response.annual_total),
        "items": _table(_RECURRING_COLUMNS, [
            [
                item.name, getattr(item.nature, "value", None), _money(item.amount), item.cadence.value,
                _money(item.monthly_equivalent), item.last_date, item.next_date, item.status.value,
                _money(item.paid_last_12_months), _recurring_note(item),
            ]
            for item in items
        ]),
    }


# ---------------------------------------------------------------------------
# Investor analytics
# ---------------------------------------------------------------------------


# Chart series and gauge settings the web page draws with: a model reads the
# metrics and verdicts beside them, and these were a third of the answer.
_CHART_ONLY = {
    ("market_conditioning", "points"),
    ("market_conditioning", "density"),
    ("regularity", "monthly"),
    ("plan", "months"),
    ("counterfactual", "order"),
}
_DISPLAY_KEYS = {"reading", "bands", "format"}


def _lean(value: Any, path: tuple = ()) -> Any:
    """Drop the chart series, the display settings and every null."""
    if isinstance(value, dict):
        return {
            key: _lean(item, (*path, key))
            for key, item in value.items()
            if item is not None and key not in _DISPLAY_KEYS and (*path[-1:], key) not in _CHART_ONLY
        }
    if isinstance(value, list):
        return [_lean(item, path) for item in value]
    return value


# ---------------------------------------------------------------------------
# Performance
# ---------------------------------------------------------------------------


_PERIOD_DAYS = {"1m": 30, "3m": 91, "6m": 182, "1y": 365, "3y": 1095, "5y": 1826}


def _period_start(period: str, today: datetime.date) -> datetime.date:
    if period == "ytd":
        return datetime.date(today.year - 1, 12, 31)
    if period == "max":
        return datetime.date(1900, 1, 1)
    return today - datetime.timedelta(days=_PERIOD_DAYS[period])


def _pocket_performance(pocket) -> dict | None:
    if pocket.gain is None:
        return None
    measured = {
        "from": pocket.start,
        "to": pocket.end,
        "value_start": _money(pocket.value_start),
        "value_end": _money(pocket.value_end),
        "net_contributions": _money(pocket.net_contributions),
        "gain": _money(pocket.gain),
        "time_weighted_return_pct": _pct(pocket.time_weighted_return * 100) if pocket.time_weighted_return is not None else None,
        "annualised_return_pct": _pct(pocket.annualised_return * 100) if pocket.annualised_return is not None else None,
        "notes": pocket.warnings or None,
    }
    return {key: value for key, value in measured.items() if value is not None}


_YEAR_COLUMNS = [
    "year", "from", "to", "complete",
    "stocks_gain", "stocks_return_pct", "crypto_gain", "crypto_return_pct", "placements_gain",
    "net_contributions", "gain", "benchmark_return_pct",
]


def _year_row(year) -> list:
    def pocket(name: str) -> tuple:
        measured = year.pockets.get(name)
        if measured is None:
            return None, None
        rate = measured.time_weighted_return
        return _money(measured.gain), _pct(rate * 100) if rate is not None else None

    stocks, crypto = pocket("stocks"), pocket("crypto")
    return [
        year.year, year.covered_from, year.end, year.complete,
        *stocks, *crypto, pocket("placements")[0],
        _money(year.net_contributions), _money(year.gain),
        _pct(year.benchmark_return * 100) if year.benchmark_return is not None else None,
    ]


# ---------------------------------------------------------------------------
# The tools
# ---------------------------------------------------------------------------


def register_tools(mcp) -> None:
    """Attach every CapitalView tool to *mcp*."""

    @tool(
        mcp,
        title="Vue d'ensemble du patrimoine",
        description=(
            "Le patrimoine net de l'utilisateur, par poche, tel que le tableau de bord l'affiche : "
            "`bank` (comptes bancaires et livrets), `stocks` et `crypto` (valeur des lignes détenues, "
            "avec `invested` au prix de revient, `profit_loss` latent, `realized_profit_loss`, "
            "`dividends`, `fees`), `broker_cash` (espèces qui dorment sur les comptes-titres et "
            "exchanges, négatif sur un compte à découvert), `placements` (assurance vie, PER, SCPI… : "
            "`net_invested` versé net des rachats, `gain`) et `assets` (biens : voiture, montres…). "
            "`share_pct` est la part de chaque poche dans `global_wealth`. À appeler en premier. "
            "`changes` donne l'évolution du total depuis le dernier relevé, le début du mois et le "
            "début de l'année — versements compris, ce n'est pas un rendement (voir get_performance). "
            "`freshness` dit de quand datent les cours et quels soldes bancaires peuvent être "
            "périmés : le signaler plutôt que présenter ces chiffres comme ceux du jour. "
            "`details` ajoute chaque compte et chaque ligne (nom du titre, quantité, valeur, prix de "
            "revient, `weight_pct` = poids dans la poche). `date` rejoue le patrimoine à une date passée."
        ),
    )
    def get_portfolio_overview(
        details: Annotated[bool, Field(description="Détail compte par compte et ligne par ligne.")] = False,
        date: Day = None,
    ) -> str:
        principal = require_scope(READ_SCOPE)
        # Validated here rather than left to the read model, which would parse it
        # deep inside and raise a Python format error at the model.
        day = _as_date(date)
        with open_session() as session:
            return _render(
                get_user_balance(
                    session,
                    principal.user_uuid,
                    principal.master_key,
                    details=details,
                    date=day.isoformat() if day else None,
                )
            )

    @tool(
        mcp,
        title="Performance des investissements",
        description=(
            "Ce que les investissements (actions, crypto, placements) ont gagné sur une période : "
            "par poche, `value_start`, `value_end`, `net_contributions` (versé net des retraits "
            "pendant la période) et `gain` = ce que la poche a produit, versements exclus. "
            "`time_weighted_return_pct` est le rendement qui neutralise le moment des versements "
            "(la bonne mesure de « comment ça a performé ») ; `annualised_return_pct` seulement sur "
            "un an ou plus. `benchmark` : ce qu'a fait l'indice de référence de l'utilisateur (MSCI "
            "World par défaut) sur les mêmes jours que la poche actions, à comparer à son "
            "`time_weighted_return_pct`. `period` : '1m', '3m', '6m', 'ytd' (depuis le 1er janvier, "
            "défaut), '1y', '3y', '5y', 'max', ou 'by_year' : une ligne par année civile, poche par "
            "poche, avec l'indice en face — la première et l'année en cours sont partielles "
            "(`complete` à false), leur % couvre les jours de `from` à `to`, jamais annualisé. "
            "Pour la valeur à l'instant T, get_portfolio_overview."
        ),
    )
    def get_performance(
        period: Annotated[
            Literal["1m", "3m", "6m", "ytd", "1y", "3y", "5y", "max", "by_year"],
            Field(description="Fenêtre mesurée jusqu'à aujourd'hui, ou 'by_year' pour chaque année civile."),
        ] = "ytd",
    ) -> str:
        principal = require_scope(READ_SCOPE)
        if period == "by_year":
            with open_session() as session:
                result = yearly_performance(session, principal.user_uuid, principal.master_key)
            return _render({
                "period": period,
                "benchmark": result.benchmark_name,
                "years": _table(_YEAR_COLUMNS, [_year_row(year) for year in result.years]),
            })

        today = datetime.date.today()
        start = _period_start(period, today)
        with open_session() as session:
            pockets = period_performance(session, principal.user_uuid, principal.master_key, start, today)
            stocks = pockets["stocks"]
            benchmark = None
            if stocks.gain is not None and stocks.start and stocks.end:
                key, name = user_benchmark(session, principal.user_uuid, principal.master_key)
                bench = benchmark_return(session, key, stocks.start, stocks.end)
                if bench is not None:
                    benchmark = {"name": name, "from": stocks.start, "to": stocks.end, "return_pct": _pct(bench * 100)}

        measured = {name: _pocket_performance(p) for name, p in pockets.items()}
        present = [p for p in pockets.values() if p.gain is not None]
        return _render({
            "period": period,
            "from": start if period != "max" else None,
            "to": today,
            "pockets": {name: body for name, body in measured.items() if body is not None},
            "benchmark": benchmark,
            "total": {
                "value_start": _money(sum((p.value_start for p in present), Decimal(0))),
                "value_end": _money(sum((p.value_end for p in present), Decimal(0))),
                "net_contributions": _money(sum((p.net_contributions for p in present), Decimal(0))),
                "gain": _money(sum((p.gain for p in present), Decimal(0))),
            } if present else None,
        })

    @tool(
        mcp,
        title="Dépenses, revenus et épargne réels",
        description=(
            "Ce qui a réellement bougé sur les comptes bancaires, chaque opération classée comme "
            "dans l'app : `income` (revenus), `expenses` (dépenses, remboursements déduits ; "
            "`recurring_expenses` + `one_off_expenses`), `saved` (mis de côté sur un livret), "
            "`invested` (envoyé vers PEA, crypto, assurance vie…), `left_after_expenses` (revenus − "
            "dépenses), `remaining` (ce qui reste après avoir aussi épargné et investi), "
            "`savings_rate_pct` = (revenus − dépenses) / revenus, `placed_rate_pct` = (épargné + "
            "investi) / revenus. Les virements entre ses propres comptes ne sont ni revenus ni "
            "dépenses. C'est LA source pour « combien je dépense / gagne / épargne par mois » : citer "
            "`monthly_median` (le mois type, qu'un mois exceptionnel ne déforme pas). "
            "`period` vide = les `months` derniers mois complets (défaut 12, jusqu'à 120, avec "
            "`by_year` pour la tendance d'une année sur l'autre) ; 'YYYY' = une année civile ; "
            "'YYYY-MM' = un mois complet, avec ses plus grosses dépenses et ses récurrents ; "
            "'current' = le mois en cours comparé aux mois habituels au même jour, avec les "
            "prélèvements encore attendus. `months` est un tableau (`columns` + `rows`). Relayer "
            "`caveats` quand il n'est pas vide."
        ),
    )
    def get_cashflow(
        period: Annotated[
            str | None,
            Field(description="Vide, 'current', 'YYYY' ou 'YYYY-MM'."),
        ] = None,
        months: Annotated[
            int, Field(ge=1, le=120, description="Sans `period` : nombre de mois complets, jusqu'au dernier.")
        ] = 12,
    ) -> str:
        principal = require_scope(READ_SCOPE)
        today = datetime.date.today()
        with open_session() as session:
            args = (session, principal.user_uuid, principal.master_key)
            if period is None:
                return _render(_recent_cashflow(real_cashflow_recent(*args, months=months, today=today)))
            if period == "current":
                return _render(_current_cashflow(real_cashflow_current(*args, today=today)))
            if _YEAR.match(period):
                year = int(period)
                if year > today.year:
                    raise ToolError(f"L'année {year} n'a pas commencé.")
                return _render(_year_cashflow(real_cashflow_year(*args, year=year, today=today)))
            if _MONTH.match(period):
                try:
                    return _render(_month_cashflow(real_cashflow_month(*args, period, today=today)))
                except PeriodNotCompletedError as exc:
                    raise ToolError(
                        f"Le mois {period} n'est pas terminé (dernier mois complet : "
                        f"{completed_period(today)}). period='current' donne le mois en cours."
                    ) from exc
        raise ToolError(f"period attend 'current', 'YYYY' ou 'YYYY-MM', reçu {period!r}.")

    @tool(
        mcp,
        title="Opérations bancaires",
        description=(
            "Les opérations bancaires ligne à ligne, les plus récentes d'abord, pour retrouver un "
            "paiement ou totaliser ce qui est parti chez un marchand : « combien chez Amazon cette "
            "année » = `search='amazon'`, `months=12`, puis lire `total_out`. Filtrer autant que "
            "possible : `search` (mots du libellé, sans accents ni casse), `type` (classement de "
            "l'app), `direction`, `min_amount`, `account` (nom du compte). `month` ('YYYY-MM') vise un "
            "mois ; sinon les `months` derniers mois, mois en cours compris (3 par défaut, 24 au "
            "plus). `total_in` / `total_out` portent sur TOUTES les opérations trouvées, pas "
            "seulement celles renvoyées, hors opérations en attente, virements internes et paires "
            "annulées. `amount` est signé (négatif = débit). `rows` est plafonné à `limit` (100 au "
            "plus) ; `truncated` le signale. Pour des totaux par mois, get_cashflow."
        ),
    )
    def list_bank_operations(
        month: Annotated[str | None, Field(description="Un mois 'YYYY-MM'.")] = None,
        months: Annotated[int, Field(ge=1, le=24, description="Sans `month` : les N derniers mois.")] = 3,
        search: Annotated[str | None, Field(description="Mots qui doivent tous figurer dans le libellé.")] = None,
        type: Annotated[
            Literal["income", "expense", "saving", "investment", "neutral"] | None,
            Field(description="Classement de l'opération dans l'app."),
        ] = None,
        direction: Annotated[Literal["in", "out"] | None, Field(description="'in' crédits, 'out' débits.")] = None,
        min_amount: Annotated[float | None, Field(ge=0, description="Montant absolu minimal, en euros.")] = None,
        account: Annotated[str | None, Field(description="Nom du compte bancaire.")] = None,
        limit: Annotated[int, Field(description="Lignes renvoyées, 100 au plus.")] = 30,
    ) -> str:
        principal = require_scope(READ_SCOPE)
        periods = [_as_month(month)] if month else _last_months(months, datetime.date.today())
        with open_session() as session:
            account_id = None
            if account:
                accounts = get_user_bank_accounts(session, principal.user_uuid, principal.master_key).accounts
                account_id = next((a.id for a in accounts if a.name.lower() == account.lower()), None)
                if account_id is None:
                    names = ", ".join(sorted(a.name for a in accounts)) or "aucun"
                    raise ToolError(f"Aucun compte bancaire nommé {account!r}. Comptes : {names}.")
            operations = list_operations(
                session, principal.user_uuid, principal.master_key, periods, account_id=account_id
            )

        terms = _words(search).split() if search else []
        wanted_type = CashflowType(type.upper()) if type else None
        matched = [
            op for op in operations
            if all(term in _words(op.label) for term in terms)
            and (wanted_type is None or op.cashflow_type is wanted_type)
            and (direction is None or op.is_credit == (direction == "in"))
            and (min_amount is None or op.amount >= Decimal(str(min_amount)))
        ]
        cap = min(max(limit, 1), MAX_OPERATIONS)
        counted = [op for op in matched if _counts(op)]
        return _render({
            "from": periods[0],
            "to": periods[-1],
            "matched": len(matched),
            "returned": min(len(matched), cap),
            "truncated": len(matched) > cap,
            "total_in": _money(sum((op.amount for op in counted if op.is_credit), Decimal(0))),
            "total_out": _money(sum((op.amount for op in counted if not op.is_credit), Decimal(0))),
            "operations": _table(_OPERATION_COLUMNS, [
                [
                    op.operation_date, op.label, _money(op.amount if op.is_credit else -op.amount),
                    op.cashflow_type.value.lower(), op.account_name, _operation_note(op),
                ]
                for op in matched[:cap]
            ]),
        })

    @tool(
        mcp,
        title="Abonnements et revenus récurrents",
        description=(
            "Les paiements récurrents (abonnements, loyer, assurances, crédits…) et les revenus "
            "récurrents (salaire, aides…) détectés sur les comptes bancaires : pour « combien me "
            "coûtent mes abonnements » ou « quand tombe mon loyer ». Par sens, `monthly_total` et "
            "`annual_total` (les récurrents actifs comptés) et `items` en tableau : montant, "
            "`cadence`, `monthly` (équivalent mensuel), dernière et prochaine échéance, `status` "
            "(active, late = échéance manquée, stale = compte pas à jour), "
            "`paid_last_12_months`. `include_inactive` ajoute les terminés, refusés et ceux pas "
            "encore confirmés."
        ),
    )
    def get_recurring(
        direction: Annotated[
            Literal["expense", "income", "both"], Field(description="Paiements, revenus, ou les deux.")
        ] = "both",
        include_inactive: Annotated[bool, Field(description="Inclure terminés, refusés, non confirmés.")] = False,
    ) -> str:
        principal = require_scope(READ_SCOPE)
        sides = {
            "expense": [RecurringDirection.EXPENSE],
            "income": [RecurringDirection.INCOME],
            "both": [RecurringDirection.EXPENSE, RecurringDirection.INCOME],
        }[direction]
        with open_session() as session:
            return _render({
                ("payments" if side is RecurringDirection.EXPENSE else "income"): _recurring_side(
                    list_recurring(session, principal.user_uuid, principal.master_key, direction=side),
                    include_inactive,
                )
                for side in sides
            })

    @tool(
        mcp,
        title="Budget déclaré",
        description=(
            "Le budget que l'utilisateur a saisi lui-même dans l'app (revenus et dépenses prévus), "
            "ramené au mois et en euros : `monthly` par sens, `monthly_balance`, `savings_rate_pct`. "
            "C'est une intention, pas une mesure : pour ce qui a vraiment été dépensé, get_cashflow. "
            "Utile pour comparer le prévu au réel. `details` détaille par catégorie et par ligne ; "
            "`flow_type` restreint à un sens."
        ),
    )
    def get_declared_budget(
        details: Annotated[bool, Field(description="Détail par catégorie et par ligne.")] = False,
        flow_type: Annotated[Literal["inflow", "outflow"] | None, Field(description="Un seul sens.")] = None,
    ) -> str:
        principal = require_scope(READ_SCOPE)
        with open_session() as session:
            return _render(
                get_user_cashflow(
                    session, principal.user_uuid, principal.master_key, details=details, flow_type=flow_type
                )
            )

    @tool(
        mcp,
        title="Courbe du patrimoine",
        description=(
            "Le patrimoine total au fil du temps sur les `days` derniers jours, ventilé par poche "
            "(`stocks` et `crypto` y comptent les espèces de leurs comptes). Pour décrire une "
            "trajectoire ou repérer un décrochage. `summary` donne le début, la fin, la variation "
            "et les extrêmes — versements compris, ce n'est pas un rendement (voir get_performance). "
            "`points` est un tableau, une valeur de clôture par pas. `granularity` 'auto' (défaut) "
            "choisit jour jusqu'à 90 jours, semaine jusqu'à deux ans, mois au-delà ; 120 points au "
            "plus, les plus récents, `truncated` le signale."
        ),
    )
    def get_wealth_history(
        days: Annotated[int, Field(ge=1, le=36500, description="Fenêtre en jours.")] = 365,
        granularity: Annotated[
            Literal["auto", "day", "week", "month"], Field(description="Pas de la série.")
        ] = "auto",
    ) -> str:
        principal = require_scope(READ_SCOPE)
        with open_session() as session:
            history = build_wealth_history(session, principal.user_uuid, principal.master_key)

        window = _within_days(history, days)
        step = _resolve_granularity(granularity, days)
        periods = len({_period_key(entry["snapshot_date"], step) for entry in window})
        points = _downsample(window, step)

        return _render({
            "granularity": step,
            "from": points[0]["snapshot_date"] if points else None,
            "to": points[-1]["snapshot_date"] if points else None,
            "truncated": periods > len(points),
            "summary": _curve_summary(points),
            "points": _table(_CURVE_COLUMNS, [_curve_row(entry) for entry in points]),
        })

    @tool(
        mcp,
        title="Achats et ventes",
        description=(
            "Les mouvements des comptes d'investissement (achats, ventes, dépôts, retraits, "
            "dividendes, frais), du plus récent au plus ancien : « qu'est-ce que j'ai acheté en "
            "mars », « quand suis-je entré sur cette ligne ». `account_type` 'stock', 'crypto' ou "
            "'all'. `since` / `until` bornent les dates (incluses) : préférer une fenêtre resserrée, "
            "la réponse est plafonnée à 200 lignes et `truncated` le signale. Les opérations "
            "bancaires sont dans list_bank_operations."
        ),
    )
    def list_investment_transactions(
        account_type: Annotated[
            Literal["stock", "crypto", "all"], Field(description="Comptes-titres, crypto, ou les deux.")
        ] = "all",
        since: Day = None,
        until: Day = None,
        limit: Annotated[int, Field(description="Lignes renvoyées, 200 au plus.")] = 50,
    ) -> str:
        principal = require_scope(READ_SCOPE)
        start, end = _as_date(since), _as_date(until)
        with open_session() as session:
            movements = list_transactions(
                session,
                principal.user_uuid,
                principal.master_key,
                account_type=account_type,
                since=start,
                until=end,
            )
        cap = min(max(limit, 1), MAX_TRANSACTIONS)
        return _render({
            "matched": len(movements),
            "returned": min(len(movements), cap),
            "truncated": len(movements) > cap,
            "transactions": _table(
                ["date", "account", "type", "symbol", "name", "quantity", "unit_price", "total", "fees", "currency"],
                [
                    [
                        m["executed_at"].date(), m["account_name"], m["type"], m["symbol"], m["name"],
                        m["amount"], m["price_per_unit"], _money(m["total_cost"]), _money(m["fees"]), m["currency"],
                    ]
                    for m in movements[:cap]
                ],
            ),
        })

    @tool(
        mcp,
        title="Projection du patrimoine",
        description=(
            "Projette le patrimoine complet sur `months` mois, à partir de `global_wealth` "
            "d'aujourd'hui. Pour « où j'en serai dans X années » et pour comparer des scénarios. "
            "Sans paramètre, chaque poche part de ce que l'historique mesure : actions, crypto et "
            "placements, le versement mensuel moyen et le rendement time-weighted annualisé ; "
            "banque, l'épargne du mois médian de l'année écoulée (revenus − dépenses − "
            "investissements) et les taux saisis sur les livrets ; biens matériels, gardés à valeur "
            "constante. `monthly_stock`, `monthly_crypto`, `monthly_bank`, `monthly_placements` "
            "fixent l'apport mensuel en euros ; `annual_return_*` le rendement annuel en décimal "
            "(0.05 = 5 %/an). `outcome` décompose l'arrivée : `starting_value`, `contributed` (versé "
            "sur la période), `growth` (produit par le rendement) et `growth_share` (part des gains "
            "dans le total, en ratio). Chaque point porte la même décomposition. `assumptions` "
            "renvoie ce qui a été retenu, d'où ça vient et les réserves : les citer, et ne jamais "
            "présenter la courbe comme une prévision."
        ),
    )
    def project_wealth(
        months: Annotated[int, Field(description="Horizon en mois, 600 au plus.")] = 120,
        monthly_stock: float | None = None,
        monthly_crypto: float | None = None,
        monthly_bank: float | None = None,
        annual_return_stock: float | None = None,
        annual_return_crypto: float | None = None,
        annual_return_bank: float | None = None,
        monthly_placements: float | None = None,
        annual_return_placements: float | None = None,
    ) -> str:
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
                annual_return_stock=annual_return_stock,
                annual_return_crypto=annual_return_crypto,
                annual_return_bank=annual_return_bank,
                monthly_placements=monthly_placements,
                annual_return_placements=annual_return_placements,
            )

        # The service answers a losing trajectory with an empty curve. Left as
        # is, that reads as "no projection available" when it actually means
        # "this ends up below what you put in" — the one outcome the user most
        # needs told.
        assumptions = projection.parameters_used
        monthly_contribution = sum(
            used.monthly_injection for used in assumptions.assets.values()
        )

        step = _projection_step(horizon)
        points = _projection_points(projection.data, step, float(monthly_contribution))

        return _render(
            {
                "months": horizon,
                "step_months": step,
                "monthly_contribution": monthly_contribution,
                "outcome": _projection_outcome(points),
                "ends_below_contributions": not projection.data,
                "note": (
                    "À ces hypothèses, le patrimoine projeté finit sous la somme "
                    "des versements : la courbe n'est pas rendue."
                    if not projection.data
                    else None
                ),
                "assumptions": {
                    "months_to_project": assumptions.months_to_project,
                    # The service adds the contribution after applying the
                    # month's return, so a euro paid in earns nothing until the
                    # month after — the conservative convention, worth stating
                    # because the other one inflates a long horizon.
                    "contribution_timing": "end_of_month",
                    "assets": {
                        _category_name(category): _assumption(used)
                        for category, used in assumptions.assets.items()
                    },
                },
                "points": points,
            }
        )

    @tool(
        mcp,
        title="Analyse du comportement d'investisseur",
        description=(
            "Diagnostic de fond sur tout l'historique boursier : écart à l'indice de référence "
            "(`counterfactual`), prix d'exécution, régularité des versements, moment des achats "
            "dans le marché, concentration, frais réels, sorties, respect du plan déclaré. "
            "`verdict` et `signals` (bon / à surveiller / mauvais) résument ; chaque bloc a son "
            "propre `verdict`, et chaque mesure sa `reliability` et sa `caveat` à respecter. "
            "Réponse volumineuse et lente : pour un diagnostic, pas pour un chiffre."
        ),
    )
    def get_investor_analytics() -> str:
        principal = require_scope(READ_SCOPE)
        with open_session() as session:
            report = build_investor_analytics(session, principal.user_uuid, principal.master_key)
        return _render(_lean(_jsonable(report)))
