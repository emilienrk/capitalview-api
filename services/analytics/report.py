"""Assembles the investor analytics payload.

One endpoint, one replay: every block shares the same daily flows, the same value
series and the same benchmark. Splitting the API per block would recompute all of
it several times per page load.
"""

from datetime import date, timedelta
from decimal import Decimal

from sqlmodel import Session

from services.analytics.behaviour import (
    MIN_MONTHS,
    MIN_PURCHASES,
    MIN_PURCHASES_FOR_DAY_OF_MONTH,
    SOLID_MONTHS,
    analyse_deposit_lag,
    analyse_deposit_regularity,
    analyse_purchase_regularity,
    purchase_amounts,
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
    blocks, benchmark_ensured = _replay_blocks(session, transactions, benchmark_key)

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


def _replay_blocks(session: Session, transactions, benchmark_key: str) -> tuple[dict, bool]:
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
        "deposit_lag": None,
        "market_conditioning": None,
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

    bridge_payload = _bridge_payload(bridge)
    return {
        "counterfactual": bridge_payload,
        "execution": _execution_payload(execution, window),
        "regularity": _regularity_payload(regularity),
        "deposit_lag": _deposit_lag_payload(
            lag,
            regularity,
            deposit_regularity,
            bridge_payload["idle_cash_opportunity"] if bridge_payload else None,
        ),
        "market_conditioning": _conditioning_payload(conditioning),
    }, True


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
