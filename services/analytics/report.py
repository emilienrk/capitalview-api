"""Assembles the investor analytics payload.

One endpoint, one replay: every block shares the same daily flows, the same value
series and the same benchmark. Splitting the API per block would recompute all of
it several times per page load.
"""

import json
from datetime import date, timedelta
from decimal import Decimal

from sqlmodel import Session, select

from models.market import MarketAsset
from services.analytics.behaviour import (
    EXIT_HORIZON_DAYS,
    MIN_EPISODES,
    MIN_MONTHS,
    MIN_PURCHASES,
    MIN_PURCHASES_FOR_DAY_OF_MONTH,
    MIN_REALISATIONS,
    SOLID_MONTHS,
    analyse_deposit_lag,
    analyse_deposit_regularity,
    analyse_exits,
    analyse_purchase_regularity,
    analyse_turnover,
    purchase_amounts,
)
from services.analytics.concentration import (
    MIN_OVERLAP,
    analyse_concentration,
    holdings_from_transactions,
)
from services.analytics.fees import (
    MIN_ORDERS as FEES_MIN_ORDERS,
    PROJECTION_RATE,
    PROJECTION_YEARS,
    SOLID_ORDERS as FEES_SOLID_ORDERS,
    TARGET_BPS,
    analyse_fees,
)
from services.analytics.plan import (
    MIN_MONTHS as PLAN_MIN_MONTHS,
    PlanError,
    analyse_plan,
)
from services.analytics.benchmark import get_benchmark_series, resolve_benchmark_key
from services.analytics.counterfactual import build_bridge
from services.analytics.execution import MIN_ORDERS, SOLID_ORDERS, analyse_execution
from services.analytics.flows import is_auto_provision, stock_external_flows
from services.analytics.prices import fill_price_gaps, get_price_matrix
from services.analytics.reliability import Metric
from services.analytics.returns import annualize, time_weighted_return, xirr
from services.analytics.timing import (
    MIN_PURCHASES as MIN_TIMING_PURCHASES,
    MIN_SESSIONS,
    analyse_market_conditioning,
)
from services.analytics.window import (
    LOOKBACK_DAYS,
    calendar_days,
    resolve_trading_days,
    resolve_window,
)
from services.encryption import decrypt_data
from services.settings import get_or_create_settings
from services.stock_account import get_all_stock_accounts_history, get_user_stock_accounts
from services.stock_transaction import get_account_transactions

_ZERO = Decimal("0")

# Under a year, a chained daily series says nothing about behaviour. Three years
# is where an annualised figure stops being mostly noise.
_MIN_DAYS = 180
_SOLID_DAYS = 1095


def build_investor_analytics(session: Session, user_uuid: str, master_key: str) -> dict:
    settings = get_or_create_settings(session, user_uuid, master_key)
    benchmark_key = resolve_benchmark_key(settings)

    accounts = get_user_stock_accounts(session, user_uuid, master_key)
    transactions = []
    for account in accounts:
        transactions.extend(get_account_transactions(session, account.id, master_key))

    history = get_all_stock_accounts_history(session, user_uuid, master_key)
    series = [(snap.snapshot_date, Decimal(snap.total_value)) for snap in history]

    # The replay blocks are driven by transactions and prices alone. They must not
    # be gated behind daily snapshots, which are rebuilt by a background job and
    # can legitimately lag behind a freshly imported ledger.
    blocks, benchmark_ensured = _replay_blocks(
        session, transactions, benchmark_key, _declared_plan(settings, master_key)
    )

    if len(series) < 2:
        blocks["investor_gap"] = None
        return {
            "period_start": series[0][0] if series else None,
            "period_end": series[-1][0] if series else None,
            "days": 0,
            "benchmark_asset_key": benchmark_key,
            "verdict": build_global_verdict(blocks),
            **blocks,
        }

    series.sort(key=lambda point: point[0])
    period_start, period_end = series[0][0], series[-1][0]
    span_days = (period_end - period_start).days

    flows_all = stock_external_flows(transactions)
    flows_real = stock_external_flows(transactions, include_auto_provisions=False)

    twr = time_weighted_return(series, flows_all)
    mwr = _money_weighted(flows_real, series[-1][1], period_end)

    average_capital = sum(value for _, value in series) / Decimal(len(series))
    auto_share = _auto_provision_share(transactions)

    gap = None
    gap_eur = None
    twr_annual = annualize(twr.total_return, span_days) if twr.total_return is not None else None
    if twr_annual is not None and mwr is not None:
        gap = mwr - twr_annual
        gap_eur = gap * average_capital

    benchmark_annual = _benchmark_annual_return(
        session, benchmark_key, period_start, period_end, span_days, benchmark_ensured
    )

    def gated(value, unit):
        return _as_metric(
            Metric.gated(
                value,
                unit=unit,
                sample_size=span_days,
                minimum=_MIN_DAYS,
                solid_at=_SOLID_DAYS,
                caveat_insufficient=(
                    f"Historique de {span_days} jours : trop court pour conclure."
                ),
                caveat_indicative=(
                    "Moins de trois ans d'historique — le signe est lisible, "
                    "la magnitude annualisée beaucoup moins."
                ),
            )
        )

    gap_metric = gated(gap, "ratio_annuel")
    gap_eur_metric = gated(gap_eur, "EUR")

    blocks["investor_gap"] = {
        "twr": gated(twr.total_return, "ratio"),
        "twr_annualised": gated(twr_annual, "ratio_annuel"),
        "benchmark_annualised": gated(benchmark_annual, "ratio_annuel"),
        "mwr": gated(mwr, "ratio_annuel"),
        "gap": gap_metric,
        "gap_eur": gap_eur_metric,
        "average_capital": round(average_capital, 2),
        "auto_provision_share": auto_share,
        # The verdict reads the gated values, never the raw ones: a gap the gate
        # just withheld must not come back as an affirmative sentence.
        "verdict": _verdict(gap_metric["value"], gap_eur_metric["value"], auto_share),
    }

    return {
        "period_start": period_start,
        "period_end": period_end,
        "days": span_days,
        "benchmark_asset_key": benchmark_key,
        "verdict": build_global_verdict(blocks),
        **blocks,
    }


def _declared_plan(settings, master_key: str) -> dict | None:
    """The stored plan, decrypted.

    get_or_create_settings returns the ORM row, which only ever carries the
    encrypted blob — reading a plaintext `investment_plan` off it silently
    yields None and the plan block vanishes without a word.
    """
    blob = getattr(settings, "investment_plan_enc", None) if settings else None
    if not blob:
        return None
    try:
        return json.loads(decrypt_data(blob, master_key))
    except Exception:
        return None


def _replay_blocks(session: Session, transactions, benchmark_key: str, declared_plan=None) -> tuple[dict, bool]:
    """Resolve the window once, then feed every replay-based block from it.

    The window backfills prices and rates over the user's own span, so this is the
    only place that talks to the market layer for these blocks. They depend on
    transactions and prices alone — never on the daily snapshots, which a
    background job rebuilds and which can legitimately lag a fresh import.
    """
    empty = {
        "counterfactual": None,
        "execution": None,
        "regularity": None,
        "turnover": None,
        "deposit_lag": None,
        "market_conditioning": None,
        "concentration": None,
        "fees": None,
        "exits": None,
        "plan": None,
    }
    window = resolve_window(session, transactions, benchmark_key)
    if window.is_empty:
        return empty, False

    keys = sorted({*window.asset_keys, benchmark_key})
    sparse = get_price_matrix(session, keys, window.start, window.end)
    days = calendar_days(window.start, window.end)
    # Two views of the same data: the sparse matrix carries real sessions, which
    # is what a monthly average must average; the filled one is only used to read
    # the terminal price of each asset.
    filled = fill_price_gaps({k: dict(v) for k, v in sparse.items()}, keys, days, session)
    price_end = {
        key: quotes[window.end] for key, quotes in filled.items() if window.end in quotes
    }

    bridge = build_bridge(window, transactions, sparse, price_end)
    execution = analyse_execution(
        transactions,
        sparse,
        trading_days=resolve_trading_days(session, window.asset_keys, window.start, window.end),
    )

    purchases = purchase_amounts(transactions)
    regularity = analyse_purchase_regularity(transactions, window)
    deposit_regularity = analyse_deposit_regularity(transactions, window)
    lag = analyse_deposit_lag(transactions)

    # The benchmark reaches a year before the window so a trailing high exists on
    # its first day. Sparse on purpose: these are the real sessions.
    benchmark_quotes = get_price_matrix(
        session,
        [benchmark_key],
        window.start - timedelta(days=LOOKBACK_DAYS),
        window.end,
    ).get(benchmark_key) or {}
    conditioning = analyse_market_conditioning(
        purchases, benchmark_quotes, sorted(benchmark_quotes)
    )

    holdings = holdings_from_transactions(transactions)
    concentration = analyse_concentration(holdings, price_end, sparse)
    fees = analyse_fees(transactions, window)
    exits = analyse_exits(transactions, sparse, benchmark_quotes)
    turnover = analyse_turnover(transactions, window, _average_capital(holdings, price_end))

    plan_payload, plan_error = _plan_block(
        declared_plan, transactions, concentration, price_end, window, benchmark_quotes
    )

    # A target allocation may name a line that is not held, so labels cover the
    # union rather than the holdings alone.
    labels = _asset_labels(
        session,
        {*window.asset_keys, *(row.asset_key for row in (plan_payload.drift if plan_payload else ()))},
    )

    bridge_payload = _bridge_payload(bridge)
    return {
        "counterfactual": bridge_payload,
        "execution": _execution_payload(execution, window),
        "regularity": _regularity_payload(regularity),
        "turnover": _turnover_payload(turnover),
        "deposit_lag": _deposit_lag_payload(
            lag,
            regularity,
            deposit_regularity,
            bridge_payload["idle_cash_opportunity"] if bridge_payload else None,
        ),
        "market_conditioning": _conditioning_payload(conditioning),
        "concentration": _concentration_payload(concentration, labels),
        "fees": _fees_payload(fees),
        "exits": _exits_payload(exits),
        "plan": _plan_payload(plan_payload, plan_error, labels),
    }, True


def _asset_labels(session: Session, asset_keys) -> dict[str, tuple[str, str]]:
    """Ticker and readable name per asset key, resolved in a single query.

    An ISIN identifies a line, it does not name it, and a correlation matrix
    labelled IE00B4L5Y983 / IE00BKM4GZ66 tells the reader nothing. Keys the market
    table does not know fall back to themselves: a technical label beats a blank.
    """
    keys = sorted({key for key in asset_keys if key})
    if not keys:
        return {}

    rows = session.exec(
        select(MarketAsset.asset_key, MarketAsset.symbol, MarketAsset.name).where(
            MarketAsset.asset_key.in_(keys)
        )
    ).all()
    known = {
        key: (symbol or key, name or symbol or key) for key, symbol, name in rows
    }
    return {key: known.get(key, (key, key)) for key in keys}


def _labelled(key: str, labels: dict[str, tuple[str, str]]) -> dict:
    symbol, name = labels.get(key, (key, key))
    return {"asset_key": key, "symbol": symbol, "name": name}


def _average_capital(holdings: dict, prices: dict) -> Decimal:
    """Portfolio value at the end of the window, as the turnover denominator."""
    return sum(
        quantity * prices[key]
        for key, quantity in holdings.items()
        if key in prices and prices[key] > _ZERO
    ) or _ZERO


def _plan_block(raw, transactions, concentration, price_end, window, benchmark_quotes):
    """Score the declared plan, or surface why it cannot be scored."""
    if not raw:
        return None, None

    purchases = [
        (day, "", amount) for day, amount in purchase_amounts(transactions)
    ]
    weights = concentration.weights if concentration else []
    portfolio_value = _average_capital(holdings_from_transactions(transactions), price_end)
    try:
        return (
            analyse_plan(raw, purchases, weights, portfolio_value, window, benchmark_quotes),
            None,
        )
    except PlanError as error:
        # A plan that cannot be scored is stated, never silently dropped.
        return None, str(error)


def _regularity_payload(regularity) -> dict | None:
    if regularity is None:
        return None

    # One sample size for the whole block: a rhythm needs both enough months to
    # look at and enough purchases to be a rhythm rather than two events.
    months = regularity.months_total if regularity.purchase_count >= MIN_PURCHASES else 0

    def gated(value, unit, *, insufficient: str):
        return _as_metric(
            Metric.gated(
                value,
                unit=unit,
                sample_size=months,
                minimum=MIN_MONTHS,
                solid_at=SOLID_MONTHS,
                caveat_insufficient=insufficient,
                caveat_indicative=(
                    "Moins de deux ans d'achats — c'est une tendance, pas une habitude établie."
                ),
            )
        )

    too_short = (
        f"{regularity.purchase_count} achats sur {regularity.months_total} mois : "
        "trop peu pour parler de rythme."
    )
    equivalent = gated(
        regularity.equivalent_monthly_purchases, "achats", insufficient=too_short
    )
    hhi = gated(regularity.temporal_hhi, "indice", insufficient=too_short)
    share = gated(regularity.invested_share, "ratio", insufficient=too_short)
    variation = gated(regularity.variation_coefficient, "ratio", insufficient=too_short)
    gap_months = gated(Decimal(regularity.longest_gap_months), "mois", insufficient=too_short)

    spread = _as_metric(
        Metric.gated(
            regularity.day_of_month_spread,
            unit="jours",
            sample_size=regularity.purchase_count,
            minimum=MIN_PURCHASES_FOR_DAY_OF_MONTH,
            solid_at=SOLID_MONTHS,
            caveat_insufficient=(
                f"{regularity.purchase_count} achats : pas assez pour lire un jour habituel."
            ),
            caveat_indicative="Moins de deux ans d'achats — indicatif.",
        )
    )

    # The measure that actually judges regularity. The monthly indicators above
    # stay exposed as illustration, but they no longer decide anything: a rhythm
    # of exactly 30 days drifts across month boundaries and used to be marked
    # down for a discipline it never broke.
    deployment = gated(
        round(regularity.deployment_gap, 4)
        if regularity.deployment_gap is not None
        else None,
        "ratio",
        insufficient=too_short,
    )

    return {
        # A heatmap is the same withheld numbers in another shape.
        "monthly": (
            [
                {"year": point.year, "month": point.month, "amount": round(point.amount, 2)}
                for point in regularity.monthly
            ]
            if equivalent["value"] is not None
            else []
        ),
        "months_total": regularity.months_total,
        "months_invested": regularity.months_invested,
        "purchase_count": regularity.purchase_count,
        "deployment_gap": deployment,
        "cadence_label": regularity.cadence.label if deployment["value"] is not None else "",
        "median_gap_days": (
            regularity.cadence.median_gap_days if deployment["value"] is not None else None
        ),
        "invested_share": share,
        "variation_coefficient": variation,
        "longest_gap_months": gap_months,
        "temporal_hhi": hhi,
        "equivalent_monthly_purchases": equivalent,
        "day_of_month_spread": spread,
        "median_day_of_month": (
            regularity.median_day_of_month if spread["value"] is not None else None
        ),
        "verdict": _regularity_verdict(regularity, deployment["value"], equivalent["value"]),
    }


# Bands on the deployment gap. Discrete orders carry a floor of about 1/(2n), so
# a few points of gap is a straight line in practice, not a defect.
_DEPLOYMENT_LINEAR = Decimal("0.05")
_DEPLOYMENT_UNEVEN = Decimal("0.15")
_DEPLOYMENT_LUMPY = Decimal("0.35")


def _regularity_verdict(regularity, deployment, equivalent) -> str:
    """Read the deployment curve, never the calendar months.

    The statement is impersonal — the subject is the observed behaviour, not the
    person — and advice, when there is any to give, comes as its own sentence.
    """
    months = regularity.months_total
    if deployment is None:
        return (
            f"{regularity.purchase_count} achats sur {months} mois : "
            "pas encore de quoi décrire une stratégie."
        )

    cadence = regularity.cadence.label
    facts = f"Sur {months} mois, {regularity.purchase_count} achats"
    facts += f", {cadence}." if cadence else "."

    if deployment <= _DEPLOYMENT_LINEAR:
        return (
            f"{facts} Le capital est déployé de façon quasi linéaire dans le temps : "
            "c'est du DCA au sens strict."
        )
    if deployment <= _DEPLOYMENT_UNEVEN:
        return (
            f"{facts} Le déploiement s'écarte un peu de la ligne droite — quelques mois "
            "pèsent plus que les autres — sans que la régularité d'ensemble soit rompue."
        )

    equivalence = ""
    if equivalent is not None:
        equivalence = (
            f" La répartition du capital équivaut à {round(equivalent, 1)} achats mensuels "
            f"égaux, sur {months} mois écoulés."
        )
    if deployment <= _DEPLOYMENT_LUMPY:
        return (
            f"{facts} Le capital est déployé par à-coups plutôt que régulièrement.{equivalence} "
            "Si l'intention est de lisser les points d'entrée, tu peux fixer un montant et "
            "une date et t'y tenir."
        )
    return (
        f"{facts} L'essentiel du capital est entré en une fois : le profil est celui d'un "
        f"investissement forfaitaire, pas d'un versement programmé.{equivalence}"
    )


def _deposit_lag_payload(lag, purchases, deposits, idle_opportunity) -> dict | None:
    if lag is None:
        return None

    measurable = lag.is_measurable
    unmatched_caveat = (
        f"{int(lag.unmatched_share * 100)} % de tes achats n'ont pas de dépôt réel en face "
        "(provisions automatiques) : la date d'entrée de ton argent est inconnue."
    )

    def gated(value):
        return _as_metric(
            Metric.gated(
                value if measurable else None,
                unit="jours",
                sample_size=lag.pairs,
                minimum=1,
                solid_at=10,
                caveat_insufficient=unmatched_caveat if lag.pairs else "Aucun dépôt à apparier.",
                caveat_indicative="Peu d'appariements — l'ordre de grandeur, pas la précision.",
            )
        )

    def variation_of(regularity, label: str):
        value = regularity.variation_coefficient if regularity else None
        months = regularity.months_total if regularity else 0
        return _as_metric(
            Metric.gated(
                value,
                unit="ratio",
                sample_size=months,
                minimum=MIN_MONTHS,
                solid_at=SOLID_MONTHS,
                caveat_insufficient=f"Pas assez de {label} pour en lire la régularité.",
                caveat_indicative="Moins de deux ans — tendance, pas preuve.",
            )
        )

    median = gated(lag.median_days)
    deposit_variation = variation_of(deposits, "dépôts")
    purchase_variation = variation_of(purchases, "achats")

    return {
        "median_days": median,
        "q1_days": gated(lag.q1_days),
        "q3_days": gated(lag.q3_days),
        "p90_days": gated(lag.p90_days),
        "matched_eur": round(lag.matched_eur, 2),
        "unmatched_eur": round(lag.unmatched_eur, 2),
        "unmatched_share": round(lag.unmatched_share, 4),
        "never_invested_eur": round(lag.never_invested_eur, 2),
        "deposit_variation": deposit_variation,
        "purchase_variation": purchase_variation,
        "idle_cash_opportunity": idle_opportunity,
        "verdict": _deposit_lag_verdict(
            median["value"],
            deposit_variation["value"],
            purchase_variation["value"],
            idle_opportunity,
            lag,
        ),
    }


def _days(value) -> str:
    """A count of days, agreed. "0 jour(s)" is not French."""
    count = round(value)
    return f"{count} jour" if abs(count) < 2 else f"{count} jours"


def _deposit_lag_verdict(median, deposit_cv, purchase_cv, idle_opportunity, lag) -> str:
    if median is None:
        return (
            "Les achats sont financés par des provisions automatiques : l'app crée le dépôt au "
            "moment de l'achat, donc le délai entre le virement réel et l'investissement n'est "
            "pas mesurable. C'est une limite de la donnée, pas un défaut de comportement."
        )

    cost = ""
    if idle_opportunity is not None and idle_opportunity != _ZERO:
        cost = f" Ce délai a coûté {abs(round(idle_opportunity))} €."

    if median <= Decimal("2"):
        return (
            f"L'argent déposé est investi en médiane en {_days(median)}. L'irrégularité "
            "éventuelle porte donc sur l'épargne, pas sur la stratégie d'investissement."
        )

    rhythms = ""
    if deposit_cv is not None and purchase_cv is not None and purchase_cv > deposit_cv:
        rhythms = (
            f" Les dépôts sont plus réguliers que les achats (variation {round(deposit_cv, 2)} "
            f"contre {round(purchase_cv, 2)})."
        )

    return (
        f"L'argent déposé attend en médiane {_days(median)} avant d'être investi.{rhythms}{cost}"
    )


def _conditioning_payload(conditioning) -> dict | None:
    if conditioning is None:
        return None

    measurable = conditioning.is_measurable
    caveat = (
        f"{conditioning.sample_size} achats sur {conditioning.sessions} séances : "
        "trop peu pour distinguer une habitude du hasard."
    )

    def gated(value):
        return _as_metric(
            Metric.gated(
                value if measurable else None,
                unit="ratio",
                sample_size=min(conditioning.sample_size, conditioning.sessions),
                minimum=min(MIN_TIMING_PURCHASES, MIN_SESSIONS),
                solid_at=MIN_SESSIONS,
                caveat_insufficient=caveat,
                caveat_indicative="Peu d'achats — le sens se lit, pas l'amplitude.",
            )
        )

    weighted = gated(conditioning.weighted_drawdown)
    show = weighted["value"] is not None

    return {
        "weighted_drawdown": weighted,
        "unconditional_drawdown": gated(conditioning.unconditional_drawdown),
        "weighted_momentum": gated(conditioning.weighted_momentum),
        "unconditional_momentum": gated(conditioning.unconditional_momentum),
        "density": _density(conditioning) if show else [],
        "points": (
            [
                {"day": day, "amount": round(amount, 2), "drawdown": round(drawdown, 4)}
                for day, amount, drawdown, _ in conditioning.purchase_states
            ]
            if show
            else []
        ),
        "yearly": (
            [{"label": label, "drawdown": round(value, 4)} for label, value in conditioning.yearly]
            if show
            else []
        ),
        "p_value": (
            round(Decimal(str(conditioning.permutation.p_value)), 4)
            if conditioning.permutation and show
            else None
        ),
        "percentile": (
            round(Decimal(str(conditioning.permutation.percentile)), 1)
            if conditioning.permutation and show
            else None
        ),
        "is_detectable": bool(show and conditioning.permutation and conditioning.permutation.is_detectable),
        "sessions": conditioning.sessions,
        "verdict": _conditioning_verdict(conditioning, weighted["value"]),
    }


_DENSITY_BINS = 12


def _density(conditioning) -> list[dict]:
    """Both distributions on one shared set of bins, as shares.

    Shares rather than counts: 40 purchases against 700 sessions would otherwise
    draw as a flat line next to a mountain.
    """
    drawdowns = [state.drawdown for state in conditioning.states]
    if not drawdowns:
        return []
    low, high = min(drawdowns), max(drawdowns)
    if high <= low:
        return []
    width = (high - low) / Decimal(_DENSITY_BINS)

    def bucket(value: Decimal) -> int:
        index = int((value - low) / width)
        return min(max(index, 0), _DENSITY_BINS - 1)

    sessions = [_ZERO] * _DENSITY_BINS
    for value in drawdowns:
        sessions[bucket(value)] += Decimal("1")

    purchases = [_ZERO] * _DENSITY_BINS
    for _, amount, drawdown, _ in conditioning.purchase_states:
        purchases[bucket(drawdown)] += amount

    session_total = sum(sessions)
    purchase_total = sum(purchases)
    return [
        {
            "centre": round(low + width * (Decimal(i) + Decimal("0.5")), 4),
            "session_share": round(sessions[i] / session_total, 4) if session_total else _ZERO,
            "purchase_share": round(purchases[i] / purchase_total, 4) if purchase_total else _ZERO,
        }
        for i in range(_DENSITY_BINS)
    ]


def _conditioning_verdict(conditioning, weighted) -> str:
    if weighted is None:
        return (
            f"{conditioning.sample_size} achats : pas de quoi dire si tu achètes les creux ou "
            "les sommets."
        )

    unconditional = conditioning.unconditional_drawdown
    mine = f"{round(weighted * 100, 1)} %"
    average = f"{round(unconditional * 100, 1)} %" if unconditional is not None else "—"
    permutation = conditioning.permutation

    if permutation is None or not permutation.is_detectable:
        return (
            f"L'euro moyen entre quand le marché est à {mine} de son plus haut ; un jour moyen, "
            f"c'est {average}. L'écart n'est pas distinguable du hasard : les achats ne sont "
            "conditionnés ni à la peur ni à l'euphorie. Ce n'est pas là que ça se joue."
        )
    if unconditional is not None and weighted > unconditional:
        return (
            f"L'euro moyen entre quand le marché est à {mine} de son plus haut. Un jour moyen, "
            f"c'est {average}. Les achats se font plus haut que le hasard (p = "
            f"{round(permutation.p_value, 3)}) : le point d'entrée suit la confirmation du "
            "marché, et cette confirmation se paie."
        )
    return (
        f"L'euro moyen entre quand le marché est à {mine} de son plus haut, contre {average} "
        f"pour un jour au hasard (p = {round(permutation.p_value, 3)}) : les achats tombent dans "
        "les creux. Sur cette durée, c'est un constat, pas une garantie que ça continue."
    )


def _bridge_payload(bridge) -> dict | None:
    if bridge is None:
        return None
    return {
        "baseline": round(bridge.baseline, 2),
        "steps": [
            {"key": step.key, "label": step.label, "amount": round(step.amount, 2)}
            for step in bridge.steps
        ],
        "residual": round(bridge.residual, 2),
        "final": round(bridge.final, 2),
        "behaviour_cost": round(bridge.behaviour_cost, 2),
        "idle_cash": round(bridge.idle_cash, 2),
        "idle_cash_opportunity": (
            round(bridge.idle_cash_opportunity, 2)
            if bridge.idle_cash_opportunity is not None
            else None
        ),
        "covered_from": bridge.covered_from,
        "covered_days": bridge.covered_days,
        "truncated": bridge.truncated,
        "order": bridge.order,
        "verdict": _bridge_verdict(bridge),
    }


def _bridge_verdict(bridge) -> str:
    cost = round(bridge.behaviour_cost)
    truncation = ""
    if bridge.truncated:
        truncation = (
            f" La comparaison ne démarre qu'au {bridge.covered_from:%d/%m/%Y} : "
            "l'indice de référence n'existait pas avant."
        )

    drag = ""
    if bridge.idle_cash_opportunity is not None and bridge.idle_cash > _ZERO:
        drag = (
            f" À côté de ça, {round(bridge.idle_cash)} € sont restés en liquidités : "
            f"placés sur l'indice, ils auraient rapporté {round(bridge.idle_cash_opportunity)} €."
        )

    if cost < 0:
        return (
            f"À capital investi égal, un robot achetant l'indice tous les mois arriverait "
            f"{abs(cost)} € plus haut. L'écart porte sur la sélection d'actifs, pas sur le moment "
            f"des achats — c'est l'écart investisseur qui juge le timing.{truncation}{drag}"
        )
    return (
        f"À capital investi égal, les décisions prises rapportent {cost} € de plus qu'un robot "
        f"achetant l'indice tous les mois. L'écart porte sur la sélection d'actifs, pas sur le "
        f"moment des achats.{truncation}{drag}"
    )


def _execution_payload(execution, window) -> dict | None:
    if execution is None or execution.sample_size == 0:
        return None

    orders = execution.sample_size
    slippage = _as_metric(
        Metric.gated(
            execution.weighted_slippage_bps,
            unit="bps",
            sample_size=orders,
            minimum=MIN_ORDERS,
            solid_at=SOLID_ORDERS,
            caveat_insufficient=f"{orders} achats : trop peu pour lire une habitude.",
            caveat_indicative="Moins de trente achats — la moyenne reste sensible à un ou deux ordres.",
        )
    )
    cost = _as_metric(
        Metric.gated(
            round(execution.cost_eur, 2) if execution.cost_eur is not None else None,
            unit="EUR",
            sample_size=orders,
            minimum=MIN_ORDERS,
            solid_at=SOLID_ORDERS,
            caveat_insufficient=f"{orders} achats : trop peu pour chiffrer un coût.",
        )
    )

    permutation = execution.permutation
    # The distribution is a picture of the same withheld numbers, so it follows
    # the gate rather than leaking a shape the metric refused to state.
    distribution = None
    if execution.quartiles and slippage["value"] is not None:
        low, q1, median, q3, high = execution.quartiles
        distribution = {
            "minimum": low,
            "q1": q1,
            "median": median,
            "q3": q3,
            "maximum": high,
        }

    return {
        "slippage_bps": slippage,
        "cost_eur": cost,
        "order_count": orders,
        "distribution": distribution,
        "p_value": round(Decimal(str(permutation.p_value)), 4) if permutation else None,
        "percentile": round(Decimal(str(permutation.percentile)), 1) if permutation else None,
        "is_detectable": bool(permutation and permutation.is_detectable),
        "verdict": _execution_verdict(slippage["value"], cost["value"], orders, permutation),
    }


def _execution_verdict(slippage_bps, cost_eur, orders: int, permutation) -> str:
    if slippage_bps is None or cost_eur is None:
        return (
            f"Seulement {orders} achats : pas de quoi dire si tu paies trop cher ou non."
        )
    if permutation is None or not permutation.is_detectable:
        return (
            f"Slippage moyen de {round(slippage_bps)} bps, que le test de permutation ne "
            "distingue pas du hasard. Le timing d'exécution ne coûte rien et ne rapporte rien : "
            "ce n'est pas là que ça se joue."
        )
    if slippage_bps > _ZERO:
        return (
            f"Sur {orders} achats, le prix payé dépasse en moyenne de {round(slippage_bps)} bps "
            f"le prix moyen du mois, soit {round(cost_eur)} €. Le test de permutation le classe "
            f"au {round(permutation.percentile)}ᵉ centile : les ordres partent systématiquement "
            "après la hausse du mois."
        )
    return (
        f"Sur {orders} achats, le prix payé est en moyenne {abs(round(slippage_bps))} bps sous le "
        f"prix moyen du mois, soit {abs(round(cost_eur))} € gagnés. Sur cette durée, c'est autant "
        "de la chance que du talent."
    )


def _benchmark_annual_return(
    session: Session,
    benchmark_key: str,
    period_start: date,
    period_end: date,
    span_days: int,
    already_ensured: bool = False,
):
    """Annualised total return of the benchmark over the exact same window.

    The benchmark is an accumulating ETF, so its quoted price already compounds
    dividends: first and last quote are all it takes.
    """
    series = get_benchmark_series(
        session, benchmark_key, period_start, period_end, ensure=not already_ensured
    )
    start_price = series.get(period_start)
    end_price = series.get(period_end)
    if not start_price or not end_price or start_price <= _ZERO:
        return None
    return annualize(end_price / start_price - Decimal("1"), span_days)


def _money_weighted(flows: dict[date, Decimal], terminal_value: Decimal, terminal_day: date):
    """XIRR over real external flows plus the terminal liquidation value.

    Deposits are negative: they leave the investor's pocket. The terminal value is
    the positive counterpart — what getting out today would return.
    """
    cashflows = [(day, -amount) for day, amount in sorted(flows.items())]
    if not cashflows:
        return None
    cashflows.append((terminal_day, terminal_value))
    return xirr(cashflows)


def _auto_provision_share(transactions) -> Decimal:
    deposits = [
        tx
        for tx in transactions
        if str(getattr(tx.type, "value", tx.type)) == "DEPOSIT"
        and str(tx.asset_key or "").upper() == "EUR"
    ]
    if not deposits:
        return _ZERO
    auto = sum(Decimal(str(tx.amount)) for tx in deposits if is_auto_provision(tx))
    total = sum(Decimal(str(tx.amount)) for tx in deposits)
    return round(auto / total, 4) if total > _ZERO else _ZERO


def _turnover_payload(turnover) -> dict | None:
    if turnover is None:
        return None
    rate = _as_metric(
        Metric.gated(
            turnover.annual_rate,
            unit="ratio_annuel",
            sample_size=int(turnover.years * 12),
            minimum=MIN_MONTHS,
            solid_at=SOLID_MONTHS,
            caveat_insufficient="Historique trop court pour un taux de rotation annuel.",
            caveat_indicative="Moins de deux ans — tendance, pas preuve.",
        )
    )
    return {
        "annual_rate": rate,
        "purchases_eur": round(turnover.purchases_eur, 2),
        "sales_eur": round(turnover.sales_eur, 2),
    }


def _concentration_payload(concentration, labels: dict) -> dict | None:
    if concentration is None:
        return None

    effective = _as_metric(
        Metric.gated(
            round(concentration.effective_positions, 2)
            if concentration.effective_positions is not None
            else None,
            unit="positions",
            sample_size=concentration.lines,
            minimum=1,
            solid_at=2,
            caveat_insufficient="Aucune ligne détenue.",
        )
    )
    # Capped at "indicatif" on purpose: two years of daily returns make a noisy
    # covariance, and a PCA over a handful of assets is sensitive (spec 2.3).
    bets = _as_metric(
        Metric.gated(
            concentration.independent_bets,
            unit="paris",
            sample_size=concentration.overlap,
            minimum=MIN_OVERLAP,
            solid_at=10**9,
            caveat_insufficient=(
                f"{concentration.overlap} rendements journaliers communs : il en faut "
                f"{MIN_OVERLAP} pour estimer une corrélation."
            ),
            caveat_indicative=(
                "Deux ans de rendements journaliers font une estimation bruitée : "
                "l'ordre de grandeur, pas la décimale."
            ),
        )
    )
    show = bets["value"] is not None

    return {
        "lines": concentration.lines,
        "effective_positions": effective,
        "independent_bets": bets,
        "weights": [
            {**_labelled(key, labels), "weight": round(weight, 4)}
            for key, weight in concentration.weights
        ],
        "correlations": (
            [
                {
                    "left": left,
                    "right": right,
                    "value": value,
                    "left_symbol": labels.get(left, (left, left))[0],
                    "right_symbol": labels.get(right, (right, right))[0],
                    "left_name": labels.get(left, (left, left))[1],
                    "right_name": labels.get(right, (right, right))[1],
                }
                for left, right, value in concentration.correlations
            ]
            if show
            else []
        ),
        "max_correlation": concentration.max_correlation if show else None,
        "overlap": concentration.overlap,
        "dropped": [_labelled(key, labels) for key in concentration.dropped],
        "verdict": _concentration_verdict(concentration, effective["value"], bets["value"]),
    }


def _concentration_verdict(concentration, effective, bets) -> str:
    lines = concentration.lines
    if bets is None:
        if effective is None:
            return "Aucune ligne détenue à analyser."
        return (
            f"{_lines(lines)} détenue(s), soit {effective} position(s) effective(s) une fois "
            "pondérées. Pas encore assez d'historique commun pour dire combien de paris "
            "réellement distincts cela représente."
        )
    correlation_note = ""
    if concentration.max_correlation is not None and concentration.max_correlation > Decimal("0.9"):
        correlation_note = (
            f" Les deux lignes les plus proches corrèlent à {concentration.max_correlation}."
        )
    if bets < Decimal("1.5") and lines > 1:
        return (
            f"{_lines(lines)} détenues. Pondérées, cela fait {effective} positions effectives, et "
            f"statistiquement {round(bets, 1)} pari indépendant : la diversification affichée est "
            f"une illusion de comptage.{correlation_note} Un ETF de plus sur le même univers n'y "
            "changerait rien ; seul un actif décorrélé le ferait."
        )
    return (
        f"{_lines(lines)} détenues, soit {effective} positions effectives et {round(bets, 1)} "
        f"paris réellement indépendants.{correlation_note}"
    )


def _lines(count: int) -> str:
    return f"{count} ligne" if count < 2 else f"{count} lignes"


def _fees_payload(fees) -> dict | None:
    if fees is None:
        return None

    def gated(value, unit, *, insufficient: str):
        return _as_metric(
            Metric.gated(
                value,
                unit=unit,
                sample_size=fees.order_count,
                minimum=FEES_MIN_ORDERS,
                solid_at=FEES_SOLID_ORDERS,
                caveat_insufficient=insufficient,
                caveat_indicative="Moins de vingt ordres — l'ordre de grandeur, pas la précision.",
            )
        )

    too_few = f"{fees.order_count} ordres : trop peu pour décrire une habitude de frais."
    threshold = gated(
        round(fees.threshold_order_size, 2) if fees.threshold_order_size is not None else None,
        "EUR",
        insufficient=too_few,
    )

    return {
        "total_fees": gated(round(fees.total_fees, 2), "EUR", insufficient=too_few),
        "fee_share": gated(
            round(fees.fee_share, 6) if fees.fee_share is not None else None,
            "ratio",
            insufficient=too_few,
        ),
        "annual_bps": gated(
            round(fees.annual_bps, 2) if fees.annual_bps is not None else None,
            "bps",
            insufficient=too_few,
        ),
        "threshold_order_size": threshold,
        "orders_below_threshold": fees.orders_below_threshold,
        # Whether grouping orders is worth recommending, not merely possible.
        "avoidable": fees.is_avoidable,
        "cost_below_threshold": round(fees.cost_below_threshold, 2),
        "invested_below_threshold": round(fees.invested_below_threshold, 2),
        "average_fee": round(fees.average_fee, 2) if fees.average_fee is not None else None,
        "average_order": round(fees.average_order, 2) if fees.average_order is not None else None,
        "order_count": fees.order_count,
        "projection_eur": fees.projection_eur,
        "projection_note": (
            f"Projection sur {PROJECTION_YEARS} ans à hypothèse constante : même cadence de "
            f"versement qu'aujourd'hui, {int(PROJECTION_RATE * 100)} % de rendement annuel. "
            "C'est le coût d'opportunité de tes frais, pas les frais eux-mêmes."
        ),
        "ter_note": fees.ter_note,
        "verdict": _fees_verdict(fees, threshold["value"]),
    }


def _fees_verdict(fees, threshold) -> str:
    """The annual charge is the verdict; the per-order threshold is calibration.

    Reading the threshold as advice contradicts the annual figure whenever the
    broker is cheap: every order sits under it and the whole load is still a
    fraction of the target. Only `is_avoidable` gets to recommend anything.
    """
    if threshold is None:
        if fees.total_fees <= _ZERO:
            return "Aucun frais d'ordre payé. " + fees.ter_note
        return f"{fees.order_count} ordres : trop peu pour dire si les frais sont un sujet."

    load = ""
    if fees.annual_bps is not None:
        load = f" La charge totale ressort à {round(fees.annual_bps)} bps par an"
        load += (
            f", sous la cible de {round(TARGET_BPS)} bps."
            if fees.annual_bps <= TARGET_BPS
            else f", au-dessus de la cible de {round(TARGET_BPS)} bps."
        )

    calibration = (
        f"Le courtier prend {round(fees.average_fee, 2)} € par ordre, ce qui place à "
        f"{round(threshold)} € la taille d'ordre en dessous de laquelle les frais d'entrée "
        f"dépassent {round(TARGET_BPS)} bps.{load}"
    )

    if not fees.is_avoidable:
        if fees.orders_below_threshold:
            return (
                f"{calibration} Le seuil par ordre est une information de calibrage, pas un "
                "problème à corriger tant que la charge annuelle reste sous la cible."
            )
        return f"{calibration} Aucun des {fees.order_count} ordres n'est sous ce seuil."

    return (
        f"{calibration} {fees.orders_below_threshold} des {fees.order_count} ordres sont sous ce "
        f"seuil et ont coûté {round(fees.cost_below_threshold)} € pour "
        f"{round(fees.invested_below_threshold)} € investis. Tu peux les regrouper."
    )


def _exits_payload(exits) -> dict | None:
    if exits is None:
        return None

    measurable = exits.is_measurable

    def gated(value, unit):
        return _as_metric(
            Metric.gated(
                value if measurable else None,
                unit=unit,
                sample_size=exits.realisations,
                minimum=MIN_REALISATIONS,
                # Never solid: a handful of sales cannot carry a firm conclusion.
                solid_at=10**9,
                caveat_insufficient=(
                    f"{exits.realisations} occasions de réalisation : trop peu pour mesurer "
                    "quoi que ce soit."
                ),
                caveat_indicative="Peu de ventes — le sens se lit, pas l'amplitude.",
            )
        )

    def episode_metric(value, unit):
        return _as_metric(
            Metric.gated(
                value if exits.has_episodes else None,
                unit=unit,
                sample_size=len(exits.episodes),
                minimum=MIN_EPISODES,
                solid_at=10**9,
                caveat_insufficient=(
                    f"{len(exits.episodes)} positions entièrement soldées : sous "
                    f"{MIN_EPISODES}, un taux de réussite ne veut rien dire."
                ),
                caveat_indicative="Peu d'épisodes clos — indicatif.",
            )
        )

    ratio = gated(round(exits.ratio, 3) if exits.ratio is not None else None, "ratio")

    return {
        "pgr": gated(round(exits.pgr, 4) if exits.pgr is not None else None, "ratio"),
        "plr": gated(round(exits.plr, 4) if exits.plr is not None else None, "ratio"),
        "ratio": ratio,
        "cost_eur": gated(
            round(exits.cost_eur, 2) if exits.cost_eur is not None else None, "EUR"
        ),
        "realisations": exits.realisations,
        "recent_sales": exits.recent_sales,
        "measured_sales": exits.measured_sales,
        "horizon_days": EXIT_HORIZON_DAYS,
        "hit_rate": episode_metric(
            round(exits.hit_rate, 4) if exits.hit_rate is not None else None, "ratio"
        ),
        "payoff_ratio": episode_metric(
            round(exits.payoff_ratio, 3) if exits.payoff_ratio is not None else None, "ratio"
        ),
        "episode_count": len(exits.episodes),
        "episodes": (
            [
                {
                    "asset_key": episode.asset_key,
                    "opened": episode.opened,
                    "closed": episode.closed,
                    "profit": round(episode.profit, 2),
                }
                for episode in exits.episodes
            ]
            if exits.has_episodes
            else []
        ),
        "verdict": _exits_verdict(exits, ratio["value"]),
    }


def _exits_verdict(exits, ratio) -> str:
    if ratio is None:
        return (
            f"{exits.realisations} ventes en tout. C'est trop peu pour mesurer quoi que ce "
            "soit — et c'est en soi l'information : le profil est celui d'un accumulateur, pas "
            "d'un arbitragiste. L'effet de disposition n'est pas le sujet ici, les métriques "
            "d'apport le sont."
        )

    cost = ""
    if exits.cost_eur is not None and exits.measured_sales:
        if exits.cost_eur > _ZERO:
            cost = (
                f" Sur {exits.measured_sales} ventes évaluables à un an, les titres vendus ont "
                f"fait mieux que l'indice ensuite : {round(exits.cost_eur)} € abandonnés."
            )
        else:
            cost = (
                f" Sur {exits.measured_sales} ventes évaluables à un an, sortir a évité "
                f"{abs(round(exits.cost_eur))} € de moins-value par rapport à l'indice."
            )

    episodes = ""
    if exits.has_episodes and exits.hit_rate is not None and exits.payoff_ratio is not None:
        episodes = (
            f" {round(exits.hit_rate * 100)} % des positions soldées sont gagnantes, et les "
            f"gagnantes rapportent {round(exits.payoff_ratio, 1)} fois ce que les perdantes "
            "coûtent."
        )

    if ratio > Decimal("1"):
        return (
            f"Les gains sont réalisés {round(ratio, 1)} fois plus volontiers que les pertes : "
            f"c'est le profil de l'effet de disposition — couper ce qui monte, garder ce qui "
            f"baisse.{cost}{episodes}"
        )
    return (
        f"Les gains ne sont pas coupés plus vite que les pertes (ratio {round(ratio, 1)})."
        f"{cost}{episodes}"
    )


def _plan_payload(plan, error: str | None, labels: dict) -> dict | None:
    if error is not None:
        return {
            "monthly_target": _ZERO,
            "periods": [],
            "since": date.today(),
            "months": [],
            "total_target": _ZERO,
            "total_invested": _ZERO,
            "adherence_ratio": _as_metric(
                Metric.gated(
                    None,
                    unit="ratio",
                    sample_size=0,
                    minimum=1,
                    solid_at=2,
                    caveat_insufficient=error,
                )
            ),
            "average_monthly": _as_metric(
                Metric.gated(
                    None, unit="EUR", sample_size=0, minimum=1, solid_at=2,
                    caveat_insufficient=error,
                )
            ),
            "drift": [],
            "drift_l1": _as_metric(
                Metric.gated(
                    None, unit="points", sample_size=0, minimum=1, solid_at=2,
                    caveat_insufficient=error,
                )
            ),
            "rebalance_eur": None,
            "under_invested_months": 0,
            "under_in_down_months": 0,
            "verdict": error,
            "error": error,
        }

    if plan is None:
        return None

    months = len(plan.months)

    def gated(value, unit):
        return _as_metric(
            Metric.gated(
                value,
                unit=unit,
                sample_size=months,
                minimum=PLAN_MIN_MONTHS,
                solid_at=12,
                caveat_insufficient=(
                    f"{months} mois complets depuis le début de ton plan : trop tôt pour juger."
                ),
                caveat_indicative="Moins d'un an de plan — tendance, pas preuve.",
            )
        )

    adherence = gated(
        round(plan.adherence_ratio, 4) if plan.adherence_ratio is not None else None, "ratio"
    )

    return {
        "monthly_target": plan.monthly_target,
        "periods": [
            {
                "since": period.since,
                "monthly_target": period.monthly_target,
                "allocation": period.allocation,
            }
            for period in plan.periods
        ],
        "since": plan.since,
        "months": [
            {
                "year": row.year,
                "month": row.month,
                "target": round(row.target, 2),
                "invested": round(row.invested, 2),
            }
            for row in plan.months
        ],
        "total_target": round(plan.total_target, 2),
        "total_invested": round(plan.total_invested, 2),
        "adherence_ratio": adherence,
        "average_monthly": gated(
            round(plan.average_monthly, 2) if plan.average_monthly is not None else None, "EUR"
        ),
        "drift": [
            {
                **_labelled(row.asset_key, labels),
                "target": round(row.target, 2),
                "actual": round(row.actual, 2),
            }
            for row in plan.drift
        ],
        "drift_l1": _as_metric(
            Metric.gated(
                round(plan.drift_l1, 2) if plan.drift_l1 is not None else None,
                unit="points",
                sample_size=len(plan.drift),
                minimum=1,
                solid_at=2,
                caveat_insufficient="Aucune allocation cible déclarée.",
            )
        ),
        "rebalance_eur": round(plan.rebalance_eur, 2) if plan.rebalance_eur is not None else None,
        "under_invested_months": plan.under_invested_months,
        "under_in_down_months": plan.under_in_down_months,
        "verdict": _plan_verdict(plan, adherence["value"]),
        "error": None,
    }


def _plan_verdict(plan, adherence) -> str:
    if adherence is None:
        return (
            f"Le plan court depuis {plan.since:%m/%Y} : pas encore assez de mois complets pour "
            "dire s'il est tenu."
        )

    drift = ""
    if plan.drift_l1 is not None and plan.drift_l1 > Decimal("10") and plan.rebalance_eur:
        drift = (
            f" L'allocation dérive de {round(plan.drift_l1)} points de la cible, soit "
            f"{round(plan.rebalance_eur)} € à rééquilibrer."
        )

    timing = ""
    if plan.under_invested_months and plan.under_in_down_months:
        share = round(plan.under_in_down_months * 100 / plan.under_invested_months)
        if share >= 50:
            timing = (
                f" Les mois sous-investis sont à {share} % des mois de baisse du marché."
            )

    periods = ""
    if len(plan.periods) > 1:
        periods = f" Le plan compte {len(plan.periods)} périodes, chaque mois étant confronté à la sienne."

    if adherence >= Decimal("0.98"):
        return (
            f"Le plan vise {round(plan.monthly_target)} €/mois investis. {round(plan.total_invested)} € "
            f"ont été investis en {len(plan.months)} mois : le plan est tenu.{periods}{drift}"
        )
    gap = round((Decimal("1") - adherence) * 100)
    return (
        f"Le plan vise {round(plan.monthly_target)} €/mois investis. {round(plan.total_invested)} € "
        f"ont été investis en {len(plan.months)} mois, soit {round(plan.average_monthly)} €/mois "
        f"réels — {gap} % sous le plan déclaré.{periods}{drift}{timing}"
    )


def build_global_verdict(blocks: dict) -> str:
    """The page's opening statement, three to five sentences.

    It reads gated payloads only. A block the gate withheld contributes nothing —
    not a hedged sentence, nothing — because a verdict written on numbers the page
    refuses to display is exactly the failure the reliability framing exists to
    prevent (and the defect M1 shipped before it was caught).

    Findings that carry euros are ranked by how many, because that is the order in
    which they are worth acting on.
    """
    # (amount, key, sentence). The key is what lets two findings that measure
    # different things be told apart once the ranking has picked them.
    costed: list[tuple[Decimal, str, str]] = []
    structural: list[str] = []

    gap = blocks.get("investor_gap")
    if gap and gap["gap_eur"]["value"] is not None:
        amount = gap["gap_eur"]["value"]
        if amount < _ZERO:
            costed.append(
                (
                    abs(amount),
                    "gap",
                    f"Le moment où l'argent est mis au travail coûte {abs(round(amount))} € par "
                    "rapport à la stratégie elle-même.",
                )
            )

    bridge = blocks.get("counterfactual")
    if bridge and bridge["behaviour_cost"] < _ZERO:
        costed.append(
            (
                abs(bridge["behaviour_cost"]),
                "bridge",
                f"À capital investi égal, un robot achetant l'indice tous les mois arriverait "
                f"{abs(round(bridge['behaviour_cost']))} € plus haut.",
            )
        )
    if bridge and bridge["idle_cash_opportunity"] and bridge["idle_cash"] > _ZERO:
        costed.append(
            (
                abs(bridge["idle_cash_opportunity"]),
                "idle_cash",
                f"{round(bridge['idle_cash'])} € dorment en liquidités ; placés sur l'indice, ils "
                f"auraient rapporté {abs(round(bridge['idle_cash_opportunity']))} €.",
            )
        )

    execution = blocks.get("execution")
    if (
        execution
        and execution["cost_eur"]["value"] is not None
        and execution["is_detectable"]
        and execution["cost_eur"]["value"] > _ZERO
    ):
        costed.append(
            (
                execution["cost_eur"]["value"],
                "execution",
                f"Les prix d'exécution coûtent {round(execution['cost_eur']['value'])} € : les "
                "ordres partent systématiquement au-dessus du prix moyen du mois.",
            )
        )

    regularity = blocks.get("regularity")
    if regularity and regularity["deployment_gap"]["value"] is not None:
        # Read on the deployment curve, not on calendar months: a 30-day rhythm
        # drifts across month boundaries without the discipline changing.
        deployment = regularity["deployment_gap"]["value"]
        if deployment > _DEPLOYMENT_LUMPY:
            structural.append(
                f"Sur {regularity['months_total']} mois, l'essentiel du capital est entré en une "
                "fois : le profil est celui d'un investissement forfaitaire."
            )
        elif deployment > _DEPLOYMENT_UNEVEN:
            structural.append(
                f"Sur {regularity['months_total']} mois, le capital est déployé par à-coups "
                "plutôt que régulièrement."
            )

    lag = blocks.get("deposit_lag")
    if lag and lag["median_days"]["value"] is not None and lag["median_days"]["value"] > Decimal("7"):
        structural.append(
            f"L'argent déposé attend en médiane {_days(lag['median_days']['value'])} entre le "
            "virement et l'investissement."
        )

    fees = blocks.get("fees")
    if fees and fees["threshold_order_size"]["value"] is not None and fees["avoidable"]:
        costed.append(
            (
                fees["cost_below_threshold"],
                "fees",
                f"{fees['orders_below_threshold']} ordres sont sous le seuil de "
                f"{round(fees['threshold_order_size']['value'])} € où les frais dépassent "
                f"25 bps : {round(fees['cost_below_threshold'])} € de frais évitables.",
            )
        )

    exits = blocks.get("exits")
    if exits and exits["cost_eur"]["value"] is not None and exits["cost_eur"]["value"] > _ZERO:
        costed.append(
            (
                exits["cost_eur"]["value"],
                "exits",
                f"Les titres vendus ont ensuite battu l'indice : "
                f"{round(exits['cost_eur']['value'])} € abandonnés en sortant.",
            )
        )

    plan = blocks.get("plan")
    if plan and plan["adherence_ratio"]["value"] is not None:
        shortfall = plan["total_target"] - plan["total_invested"]
        if shortfall > _ZERO:
            costed.append(
                (
                    shortfall,
                    "plan",
                    f"{round(shortfall)} € de moins ont été investis que ce que le plan déclaré "
                    "prévoyait.",
                )
            )

    concentration = blocks.get("concentration")
    if concentration and concentration["independent_bets"]["value"] is not None:
        bets = concentration["independent_bets"]["value"]
        if bets < Decimal("1.5") and concentration["lines"] > 1:
            structural.append(
                f"Les {concentration['lines']} lignes détenues ne font que "
                f"{round(bets, 1)} pari indépendant : la diversification affichée est une "
                "illusion de comptage."
            )

    conditioning = blocks.get("market_conditioning")
    if conditioning and conditioning["weighted_drawdown"]["value"] is not None:
        if conditioning["is_detectable"]:
            structural.append(conditioning["verdict"].split(". ", 1)[-1])
        else:
            structural.append(
                "Le moment des achats dans le cycle de marché n'est pas distinguable du "
                "hasard : ce n'est pas là que ça se joue."
            )

    costed.sort(key=lambda item: item[0], reverse=True)
    kept = costed[:3]
    sentences = [text for _, _, text in kept] + structural[:2]

    # Two figures that read as a contradiction unless the difference is named:
    # the robot judges which assets were bought, the investor gap judges when.
    keys = {key for _, key, _ in kept}
    if {"bridge", "gap"} <= keys:
        sentences.append(
            "Les deux montants ne mesurent pas la même chose : le robot juge la sélection "
            "d'actifs, l'écart investisseur juge le moment des versements."
        )

    if not sentences:
        return (
            "Pas encore assez d'historique pour dire quoi que ce soit d'utile sur ce "
            "comportement d'investissement. La page préfère se taire que d'inventer un verdict — "
            "reviens avec quelques mois d'achats de plus."
        )
    return " ".join(sentences[:6])


def _as_metric(metric: Metric) -> dict:
    return {
        "value": metric.value,
        "unit": metric.unit,
        "sample_size": metric.sample_size,
        "reliability": metric.reliability.value,
        "caveat": metric.caveat,
    }


def _verdict(gap, gap_eur, auto_share: Decimal) -> str:
    if gap is None or gap_eur is None:
        return (
            "Pas encore assez d'historique pour séparer ta performance de celle de ta stratégie."
        )
    if auto_share > Decimal("0.30"):
        provision_note = (
            f" {int(auto_share * 100)} % des dépôts sont des provisions automatiques : "
            "la date réelle d'entrée de l'argent est inconnue, ce chiffre est à lire avec réserve."
        )
    else:
        provision_note = ""
    if gap < _ZERO:
        return (
            f"La stratégie fait mieux que l'investisseur. L'écart, rapporté au capital moyen, "
            f"représente {round(gap_eur)} €. Il ne vient pas du choix des actifs mais du moment "
            f"où l'argent est mis au travail.{provision_note}"
        )
    return (
        f"Le moment des versements a rapporté {round(gap_eur)} € par rapport à la stratégie "
        f"elle-même. Sur cette durée, c'est autant de la chance que du talent.{provision_note}"
    )
