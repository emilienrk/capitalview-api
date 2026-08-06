"""Assembles the investor analytics payload.

One endpoint, one replay: every block shares the same daily flows, the same value
series and the same benchmark. Splitting the API per block would recompute all of
it several times per page load.
"""

import json
from datetime import date, timedelta
from decimal import Decimal

from sqlmodel import Session

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
    purchases_by_asset,
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
from services.analytics.labels import label_of, resolve_asset_labels
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

    bridge_payload = _bridge_payload(bridge)
    # One lookup for every key any block will display: the ISIN is the join, the
    # name and the ticker are what the reader gets.
    labels = resolve_asset_labels(
        session,
        [
            *(key for key, _ in (concentration.weights if concentration else ())),
            *(concentration.dropped if concentration else ()),
            *(row.asset_key for row in (getattr(plan_payload, "drift", None) or ())),
            # A line bought only during an old period appears nowhere else, and
            # would otherwise show up in that period's table as a bare ISIN.
            *(
                key
                for outcome in (getattr(plan_payload, "outcomes", None) or ())
                for key in outcome.flow_shares
            ),
        ],
    )
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

    # With the asset key, not without it: blanking it here is what kept the plan
    # from ever saying whether an allocation was followed while it was in force.
    purchases = purchases_by_asset(transactions)
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
    # The measure that actually judges regularity: distance to a straight-line
    # deployment, which has no notion of a calendar month and so cannot be fooled
    # by one. The monthly figures below it only illustrate.
    deployment = gated(regularity.deployment_gap, "ratio", insufficient=too_short)
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
        "cadence_label": regularity.cadence_label,
        "median_gap_days": regularity.median_gap_days,
        "invested_share": share,
        "variation_coefficient": variation,
        "longest_gap_months": gap_months,
        "temporal_hhi": hhi,
        "equivalent_monthly_purchases": equivalent,
        "day_of_month_spread": spread,
        "median_day_of_month": (
            regularity.median_day_of_month if spread["value"] is not None else None
        ),
        "verdict": _regularity_verdict(regularity, equivalent["value"], spread["value"]),
    }


def _regularity_verdict(regularity, equivalent, spread) -> str:
    if equivalent is None:
        return (
            f"{regularity.purchase_count} achats sur {regularity.months_total} mois : "
            "pas encore de quoi dire quelle est ta stratégie réelle."
        )

    months = regularity.months_total
    rounded = round(equivalent, 1)
    day_note = ""
    if spread is not None and regularity.median_day_of_month is not None:
        if spread <= Decimal("3"):
            day_note = (
                f" Tes ordres tombent autour du {regularity.median_day_of_month} du mois : "
                "ça, c'est une habitude."
            )
        else:
            day_note = " Le jour du mois, lui, est au hasard : tu achètes quand tu y penses."

    if equivalent >= Decimal(months) * Decimal("0.8"):
        return (
            f"Tu as investi sur {regularity.months_invested} des {months} mois, et de façon "
            f"régulière : ton capital équivaut à {rounded} achats mensuels égaux. "
            f"C'est bien du DCA.{day_note}"
        )
    return (
        f"Tu penses peut-être faire du DCA. Sur {months} mois tu as investi "
        f"{regularity.months_invested} fois, et la répartition de ton capital équivaut à "
        f"{rounded} achats mensuels égaux, pas {months}. Tu fais des achats opportunistes."
        f"{day_note}"
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
        "unpaired_deposits_eur": round(lag.unpaired_deposits_eur, 2),
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


def _deposit_lag_verdict(median, deposit_cv, purchase_cv, idle_opportunity, lag) -> str:
    if median is None:
        return (
            "Tes achats sont financés par des provisions automatiques : l'app crée le dépôt au "
            "moment de l'achat, donc le délai entre ton virement réel et ton investissement n'est "
            "pas mesurable. Ce n'est pas un défaut de ta part, c'est une limite de la donnée."
        )

    cost = ""
    if idle_opportunity is not None and idle_opportunity != _ZERO:
        cost = f" Ce délai t'a coûté {abs(round(idle_opportunity))} €."

    if median <= Decimal("2"):
        return (
            f"Ton argent est investi en médiane en {round(median)} jour(s). Ton irrégularité "
            "éventuelle est celle de ton épargne, pas de ta stratégie — ne cherche pas à corriger "
            "le mauvais comportement."
        )

    rhythms = ""
    if deposit_cv is not None and purchase_cv is not None and purchase_cv > deposit_cv:
        rhythms = (
            f" Tes dépôts sont plus réguliers que tes achats (variation {round(deposit_cv, 2)} "
            f"contre {round(purchase_cv, 2)}) : ta discipline s'arrête au virement."
        )

    return (
        f"Ton argent dort en médiane {round(median)} jours avant d'être investi.{rhythms}{cost}"
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
            f"Ton euro moyen entre quand le marché est à {mine} de son plus haut ; un jour moyen, "
            f"c'est {average}. L'écart n'est pas distinguable du hasard : tes achats ne sont "
            "conditionnés ni à la peur ni à l'euphorie. Ce n'est pas là qu'il faut chercher."
        )
    if unconditional is not None and weighted > unconditional:
        return (
            f"Ton euro moyen entre quand le marché est à {mine} de son plus haut. Un jour moyen, "
            f"c'est {average}. Tu achètes plus haut que le hasard (p = "
            f"{round(permutation.p_value, 3)}) : tu attends la confirmation, et la confirmation "
            "se paie."
        )
    return (
        f"Ton euro moyen entre quand le marché est à {mine} de son plus haut, contre {average} "
        f"pour un jour au hasard (p = {round(permutation.p_value, 3)}) : tu achètes dans les "
        "creux. Sur cette durée, c'est un constat, pas une garantie que ça continue."
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
        # The day the two portfolios are compared at — the end of the covered
        # window, not today: a truncated bridge stops earlier.
        "valued_at": bridge.covered_from + timedelta(days=bridge.covered_days),
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
            f"À capital investi égal, un robot qui aurait acheté l'indice tous les mois, sans "
            f"jamais réfléchir, aurait {abs(cost)} € de plus que toi.{truncation}{drag}"
        )
    return (
        f"À capital investi égal, tes décisions te rapportent {cost} € de plus qu'un robot qui "
        f"aurait acheté l'indice tous les mois.{truncation}{drag}"
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
            f"Slippage moyen de {round(slippage_bps)} bps, mais le test de permutation ne le "
            "distingue pas du hasard. Ton timing d'exécution ne te coûte rien et ne te rapporte "
            "rien : ce n'est pas là qu'il faut chercher."
        )
    if slippage_bps > _ZERO:
        return (
            f"Sur {orders} achats, tu paies en moyenne {round(slippage_bps)} bps au-dessus du prix "
            f"moyen du mois, soit {round(cost_eur)} €. Le test de permutation le classe au "
            f"{round(permutation.percentile)}ᵉ centile : tu achètes systématiquement après la hausse."
        )
    return (
        f"Sur {orders} achats, tu paies en moyenne {abs(round(slippage_bps))} bps sous le prix moyen "
        f"du mois, soit {abs(round(cost_eur))} € gagnés. Sur cette durée, c'est autant de la chance "
        "que du talent."
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


def _concentration_payload(concentration, labels) -> dict | None:
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
            {**label_of(labels, key).as_dict(), "weight": round(weight, 4)}
            for key, weight in concentration.weights
        ],
        # The ISIN stays the key the UI matches on; the name and the ticker ride
        # along so the axes of the matrix can be read without a lookup table.
        "correlations": (
            [
                {
                    "left": left,
                    "right": right,
                    "value": value,
                    "left_symbol": label_of(labels, left).symbol,
                    "right_symbol": label_of(labels, right).symbol,
                    "left_name": label_of(labels, left).name,
                    "right_name": label_of(labels, right).name,
                }
                for left, right, value in concentration.correlations
            ]
            if show
            else []
        ),
        "max_correlation": concentration.max_correlation if show else None,
        "overlap": concentration.overlap,
        "dropped": [label_of(labels, key).as_dict() for key in concentration.dropped],
        "verdict": _concentration_verdict(concentration, effective["value"], bets["value"]),
    }


def _concentration_verdict(concentration, effective, bets) -> str:
    lines = concentration.lines
    if bets is None:
        if effective is None:
            return "Aucune ligne détenue à analyser."
        return (
            f"Tu détiens {lines} ligne(s), soit {effective} position(s) effective(s) une fois "
            "pondérées. Pas encore assez d'historique commun pour dire combien de paris "
            "réellement distincts ça représente."
        )
    correlation_note = ""
    if concentration.max_correlation is not None and concentration.max_correlation > Decimal("0.9"):
        correlation_note = (
            f" Tes deux lignes les plus proches corrèlent à {concentration.max_correlation}."
        )
    if bets < Decimal("1.5") and lines > 1:
        return (
            f"Tu détiens {lines} lignes. Pondérées, ça fait {effective} positions effectives. "
            f"Statistiquement, ça fait {round(bets, 1)} pari indépendant : ta diversification est une "
            f"illusion de comptage.{correlation_note} Ajouter un ETF de plus sur le même univers "
            "ne changera rien ; seul un actif décorrélé le ferait."
        )
    return (
        f"Tu détiens {lines} lignes, soit {effective} positions effectives et {round(bets, 1)} "
        f"paris réellement indépendants.{correlation_note}"
    )


def _fees_payload(fees) -> dict | None:
    if fees is None:
        return None

    # Fee figures are gated on orders that actually carry a fee, not on orders.
    # A ledger imported without a fee column has no fees to describe, and
    # reporting a confident total over it states a floor as if it were the sum.
    def gated(value, unit, *, insufficient: str):
        return _as_metric(
            Metric.gated(
                value,
                unit=unit,
                sample_size=fees.orders_with_fee,
                minimum=FEES_MIN_ORDERS,
                solid_at=FEES_SOLID_ORDERS,
                caveat_insufficient=insufficient,
                caveat_indicative="Moins de vingt ordres facturés — l'ordre de grandeur, pas la précision.",
            )
        )

    too_few = (
        f"{fees.orders_with_fee} ordre(s) avec des frais renseignés sur {fees.order_count} : "
        "trop peu pour décrire une habitude de frais."
    )
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
        # Whether the small orders are a problem worth acting on, or only a
        # calibration figure: below the target the annual load is a rounding
        # error, however many orders sit under the threshold.
        "avoidable": (
            fees.annual_bps is not None and fees.annual_bps > TARGET_BPS
        ),
        "orders_below_threshold": fees.orders_below_threshold,
        "cost_below_threshold": round(fees.cost_below_threshold, 2),
        "invested_below_threshold": round(fees.invested_below_threshold, 2),
        "average_fee": round(fees.average_fee, 2) if fees.average_fee is not None else None,
        "average_order": round(fees.average_order, 2) if fees.average_order is not None else None,
        "order_count": fees.order_count,
        "orders_with_fee": fees.orders_with_fee,
        "fee_coverage": round(fees.coverage, 4),
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
    if fees.orders_with_fee == 0:
        # Not "you pay nothing": a ledger with no fees recorded and a broker that
        # charges none look identical from here, and only one of them is good news.
        return (
            f"Aucun frais renseigné sur tes {fees.order_count} ordres. Soit ton courtier ne "
            "t'en prend pas, soit ils n'ont pas été saisis — dans le second cas ce bloc ne peut "
            "rien dire. " + fees.ter_note
        )
    if threshold is None:
        return f"{fees.order_count} ordres : trop peu pour dire si tes frais sont un sujet."

    # Partial data makes every total a floor, and the sentence has to say so
    # before quoting one.
    partial = ""
    if fees.orders_with_fee < fees.order_count:
        partial = (
            f" Attention : seuls {fees.orders_with_fee} de tes {fees.order_count} ordres portent "
            "des frais renseignés, donc ce total est un plancher, pas ta facture."
        )

    if fees.orders_below_threshold == 0:
        return (
            f"Ton courtier te prend {round(fees.average_fee, 2)} € par ordre facturé. En dessous "
            f"de {round(threshold)} € par ordre tu dépasserais 25 bps de frais d'entrée : aucun "
            f"de tes ordres facturés n'est sous ce seuil.{partial}"
        )

    detail = (
        f"Ton courtier te prend en moyenne {round(fees.average_fee, 2)} € par ordre facturé. En "
        f"dessous de {round(threshold)} € par ordre, tu dépasses 25 bps de frais d'entrée. "
        f"{fees.orders_below_threshold} de tes {fees.order_count} ordres sont sous ce seuil — ils "
        f"t'ont coûté {round(fees.cost_below_threshold)} € pour "
        f"{round(fees.invested_below_threshold)} € investis."
    )
    # "Group your orders" is advice, and it must not contradict the tile above
    # it: below the target the annual load is a rounding error, however many
    # orders sit under a threshold derived from the user's own average fee.
    if fees.annual_bps is not None and fees.annual_bps > TARGET_BPS:
        return f"{detail} Regroupe-les.{partial}"
    return (
        f"{detail} Mais ta charge annuelle reste sous les 25 bps visés "
        f"({round(fees.annual_bps)} bps) : c'est un calibrage, pas un problème à "
        f"corriger.{partial}"
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
            f"Tu as vendu {exits.realisations} fois. C'est trop peu pour mesurer quoi que ce "
            "soit — et c'est en soi l'information : tu es un accumulateur, pas un arbitragiste. "
            "L'effet de disposition n'est pas ton problème, les métriques d'apport le sont."
        )

    cost = ""
    if exits.cost_eur is not None and exits.measured_sales:
        if exits.cost_eur > _ZERO:
            cost = (
                f" Sur {exits.measured_sales} ventes évaluables à un an, ce que tu as vendu a fait "
                f"mieux que l'indice ensuite : {round(exits.cost_eur)} € abandonnés."
            )
        else:
            cost = (
                f" Sur {exits.measured_sales} ventes évaluables à un an, sortir t'a évité "
                f"{abs(round(exits.cost_eur))} € de moins-value par rapport à l'indice."
            )

    episodes = ""
    if exits.has_episodes and exits.hit_rate is not None and exits.payoff_ratio is not None:
        episodes = (
            f" Tu as raison {round(exits.hit_rate * 100)} % du temps, et tes gagnantes rapportent "
            f"{round(exits.payoff_ratio, 1)} fois ce que tes perdantes coûtent."
        )

    if ratio > Decimal("1"):
        return (
            f"Tu réalises tes gains {round(ratio, 1)} fois plus volontiers que tes pertes : tu "
            f"coupes ce qui monte et gardes ce qui baisse.{cost}{episodes}"
        )
    return (
        f"Tu ne coupes pas tes gains plus vite que tes pertes (ratio {round(ratio, 1)}).{cost}"
        f"{episodes}"
    )


def _plan_payload(plan, error: str | None, labels) -> dict | None:
    if error is not None:
        return {
            "monthly_target": _ZERO,
            "since": date.today(),
            "periods": [],
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
        # The target in force today, not the one the plan opened with.
        "monthly_target": plan.monthly_target,
        "since": plan.since,
        # Every period as declared, oldest first, each with what was actually
        # done while it ran. A plan that never changed has exactly one, so the UI
        # reads a single shape either way.
        "periods": [
            {
                "since": outcome.since,
                "until": outcome.until,
                "monthly_target": outcome.monthly_target,
                "allocation": dict(outcome.allocation),
                "months": outcome.months,
                "target_eur": round(outcome.target_eur, 2),
                "invested_eur": round(outcome.invested_eur, 2),
                "adherence_ratio": (
                    round(outcome.adherence_ratio, 4)
                    if outcome.adherence_ratio is not None
                    else None
                ),
                "flow_drift_l1": (
                    round(outcome.flow_drift_l1, 2)
                    if outcome.flow_drift_l1 is not None
                    else None
                ),
                "flows": [
                    {
                        **label_of(labels, key).as_dict(),
                        "target": round(outcome.allocation.get(key, _ZERO), 2),
                        "actual": round(share, 2),
                    }
                    for key, share in sorted(
                        outcome.flow_shares.items(), key=lambda pair: -pair[1]
                    )
                ],
            }
            for outcome in plan.outcomes
        ],
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
                **label_of(labels, row.asset_key).as_dict(),
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
            f"Ton plan court depuis {plan.since:%m/%Y} : pas encore assez de mois complets pour "
            "dire si tu le tiens."
        )

    drift = ""
    if plan.drift_l1 is not None and plan.drift_l1 > Decimal("10") and plan.rebalance_eur:
        drift = (
            f" Ton allocation dérive de {round(plan.drift_l1)} points de ta cible : "
            f"{round(plan.rebalance_eur)} € à rééquilibrer."
        )

    # A revision the user only half-applied: the amount moved, the split did not.
    # The portfolio drift above cannot say this — it reads today's holdings, which
    # the market has reshaped since.
    reallocation = ""
    current = plan.outcomes[-1] if plan.outcomes else None
    if (
        len(plan.periods) > 1
        and current is not None
        and current.flow_drift_l1 is not None
        and current.flow_drift_l1 > Decimal("10")
    ):
        reallocation = (
            f" Depuis {current.since:%m/%Y} tes achats s'écartent de "
            f"{round(current.flow_drift_l1)} points de la répartition que tu as déclarée pour "
            "cette période : le montant a changé, la répartition non."
        )

    timing = ""
    if plan.under_invested_months and plan.under_in_down_months:
        share = round(plan.under_in_down_months * 100 / plan.under_invested_months)
        if share >= 50:
            timing = (
                f" Et les mois où tu as sous-investi sont à {share} % des mois de baisse du "
                "marché."
            )

    promise = _plan_promise(plan)
    if adherence >= Decimal("0.98"):
        return (
            f"{promise} Tu as investi {round(plan.total_invested)} € en "
            f"{len(plan.months)} mois : tu le tiens.{reallocation}{drift}"
        )
    gap = round((Decimal("1") - adherence) * 100)
    return (
        f"{promise} Tu as investi {round(plan.total_invested)} € en {len(plan.months)} mois, "
        f"soit {round(plan.average_monthly)} €/mois réels — {gap} % sous ton propre "
        f"plan.{reallocation}{drift}{timing}"
    )


def _plan_promise(plan) -> str:
    """What the plan asked for, in one sentence.

    A plan in several periods cannot be summarised by one monthly amount: saying
    "600 €/mois" next to a total that also covers months promised at 200 would
    read as a shortfall the user never had. The revisions are named instead, and
    the total is what the adherence ratio actually divides by.
    """
    if len(plan.periods) < 2:
        return f"Ton plan dit {round(plan.monthly_target)} €/mois investis."

    steps = " puis ".join(
        f"{round(period.monthly_target)} € depuis {period.since:%m/%Y}"
        for period in plan.periods
    )
    return (
        f"Ton plan a changé {len(plan.periods) - 1} fois — {steps} — soit "
        f"{round(plan.total_target)} € promis sur {len(plan.months)} mois complets."
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
    costed: list[tuple[Decimal, str]] = []
    structural: list[str] = []

    gap = blocks.get("investor_gap")
    if gap and gap["gap_eur"]["value"] is not None:
        amount = gap["gap_eur"]["value"]
        if amount < _ZERO:
            costed.append(
                (
                    abs(amount),
                    f"Le moment où tu mets l'argent te coûte {abs(round(amount))} € par rapport "
                    "à ta propre stratégie.",
                )
            )

    bridge = blocks.get("counterfactual")
    if bridge and bridge["behaviour_cost"] < _ZERO:
        costed.append(
            (
                abs(bridge["behaviour_cost"]),
                f"À capital investi égal, un robot achetant l'indice tous les mois aurait "
                f"{abs(round(bridge['behaviour_cost']))} € de plus que toi.",
            )
        )
    if bridge and bridge["idle_cash_opportunity"] and bridge["idle_cash"] > _ZERO:
        costed.append(
            (
                abs(bridge["idle_cash_opportunity"]),
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
                f"Tes prix d'exécution te coûtent {round(execution['cost_eur']['value'])} € : tu "
                "achètes systématiquement au-dessus du prix moyen du mois.",
            )
        )

    regularity = blocks.get("regularity")
    if regularity and regularity["equivalent_monthly_purchases"]["value"] is not None:
        equivalent = regularity["equivalent_monthly_purchases"]["value"]
        months = regularity["months_total"]
        if equivalent < Decimal(months) * Decimal("0.6"):
            structural.append(
                f"Ta répartition dans le temps équivaut à {round(equivalent, 1)} achats mensuels "
                f"égaux sur {months} mois : ce que tu appelles régularité n'en est pas."
            )

    lag = blocks.get("deposit_lag")
    if lag and lag["median_days"]["value"] is not None and lag["median_days"]["value"] > Decimal("7"):
        structural.append(
            f"Ton argent attend en médiane {round(lag['median_days']['value'])} jours entre le "
            "virement et l'investissement."
        )

    fees = blocks.get("fees")
    if fees and fees["threshold_order_size"]["value"] is not None and fees["orders_below_threshold"]:
        costed.append(
            (
                fees["cost_below_threshold"],
                f"{fees['orders_below_threshold']} de tes ordres sont sous le seuil de "
                f"{round(fees['threshold_order_size']['value'])} € où les frais dépassent "
                f"25 bps : {round(fees['cost_below_threshold'])} € de frais évitables.",
            )
        )

    exits = blocks.get("exits")
    if exits and exits["cost_eur"]["value"] is not None and exits["cost_eur"]["value"] > _ZERO:
        costed.append(
            (
                exits["cost_eur"]["value"],
                f"Ce que tu as vendu a ensuite battu l'indice : "
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
                    f"Tu as investi {round(shortfall)} € de moins que ce que ton propre plan "
                    "prévoyait.",
                )
            )

    concentration = blocks.get("concentration")
    if concentration and concentration["independent_bets"]["value"] is not None:
        bets = concentration["independent_bets"]["value"]
        if bets < Decimal("1.5") and concentration["lines"] > 1:
            structural.append(
                f"Tes {concentration['lines']} lignes ne font que {bets} pari indépendant : ta "
                "diversification est une illusion de comptage."
            )

    conditioning = blocks.get("market_conditioning")
    if conditioning and conditioning["weighted_drawdown"]["value"] is not None:
        if conditioning["is_detectable"]:
            structural.append(conditioning["verdict"].split(". ", 1)[-1])
        else:
            structural.append(
                "Le moment de tes achats dans le cycle de marché n'est pas distinguable du "
                "hasard : ce n'est pas là qu'il faut chercher."
            )

    costed.sort(key=lambda item: item[0], reverse=True)
    sentences = [text for _, text in costed[:3]] + structural[:2]

    if not sentences:
        return (
            "Pas encore assez d'historique pour dire quoi que ce soit d'utile sur ton "
            "comportement. Reviens quand tu auras quelques mois d'achats derrière toi — la page "
            "préfère se taire que d'inventer un verdict."
        )
    return " ".join(sentences[:5])


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
            f" {int(auto_share * 100)} % de tes dépôts sont des provisions automatiques : "
            "la date réelle d'entrée de ton argent est inconnue, ce chiffre est à lire avec réserve."
        )
    else:
        provision_note = ""
    if gap < _ZERO:
        return (
            f"Ta stratégie fait mieux que toi. L'écart, sur ton capital moyen, représente "
            f"{round(gap_eur)} €. Il ne vient pas de tes choix d'actifs mais du moment où "
            f"tu mets l'argent.{provision_note}"
        )
    return (
        f"Le moment où tu investis t'a rapporté {round(gap_eur)} € par rapport à ta propre "
        f"stratégie. Sur cette durée, c'est autant de la chance que du talent.{provision_note}"
    )
