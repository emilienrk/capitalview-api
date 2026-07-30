"""Assembles the investor analytics payload.

One endpoint, one replay: every block shares the same daily flows, the same value
series and the same benchmark. Splitting the API per block would recompute all of
it several times per page load.
"""

from datetime import date
from decimal import Decimal

from sqlmodel import Session

from services.analytics.benchmark import get_benchmark_series, resolve_benchmark_key
from services.analytics.counterfactual import build_bridge
from services.analytics.execution import MIN_ORDERS, SOLID_ORDERS, analyse_execution
from services.analytics.flows import is_auto_provision, stock_external_flows
from services.analytics.prices import fill_price_gaps, get_price_matrix
from services.analytics.reliability import Metric
from services.analytics.returns import annualize, time_weighted_return, xirr
from services.analytics.window import calendar_days, resolve_trading_days, resolve_window
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
    counterfactual, execution, benchmark_ensured = _replay_blocks(
        session, transactions, benchmark_key
    )

    if len(series) < 2:
        return {
            "period_start": series[0][0] if series else None,
            "period_end": series[-1][0] if series else None,
            "days": 0,
            "benchmark_asset_key": benchmark_key,
            "investor_gap": None,
            "counterfactual": counterfactual,
            "execution": execution,
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

    return {
        "period_start": period_start,
        "period_end": period_end,
        "days": span_days,
        "benchmark_asset_key": benchmark_key,
        "counterfactual": counterfactual,
        "execution": execution,
        "investor_gap": {
            "twr": gated(twr.total_return, "ratio"),
            "twr_annualised": gated(twr_annual, "ratio_annuel"),
            "benchmark_annualised": gated(benchmark_annual, "ratio_annuel"),
            "mwr": gated(mwr, "ratio_annuel"),
            "gap": gap_metric,
            "gap_eur": gap_eur_metric,
            "average_capital": round(average_capital, 2),
            "auto_provision_share": auto_share,
            # The verdict reads the gated values, never the raw ones: a gap the
            # gate just withheld must not come back as an affirmative sentence.
            "verdict": _verdict(gap_metric["value"], gap_eur_metric["value"], auto_share),
        },
    }


def _replay_blocks(session: Session, transactions, benchmark_key: str):
    """Resolve the window once, then feed both replay-based blocks from it.

    The window backfills prices and rates over the user's own span, so this is the
    only place that talks to the market layer for these two blocks.
    """
    window = resolve_window(session, transactions, benchmark_key)
    if window.is_empty:
        return None, None, False

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
    return _bridge_payload(bridge), _execution_payload(execution, window), True


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
