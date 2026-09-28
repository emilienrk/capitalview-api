"""Defaults a wealth projection should start from, measured rather than guessed.

Without them `services/projection` falls back on invested / months and value /
invested: the first counts rotated positions as deposits, the second treats a
euro deposited last week as compounding since day one.

- Return: annualised TWR, which neutralises the timing of deposits. XIRR would
  bake a lucky or unlucky entry sequence into every projected month.
- Contribution: net external flows from the ledger, auto-provisions included —
  their date is synthetic, which a solver minds and an average over years does
  not.

Fragile or extreme figures come back with a warning, never silently altered.
"""

import datetime
from dataclasses import dataclass, field
from decimal import Decimal

from sqlmodel import Session

from services.analytics.flows import stock_external_flows
from services.analytics.returns import annualize, time_weighted_return

# 365.25 / 12, so a month means the same thing here as it does in `annualize`.
DAYS_PER_MONTH = Decimal("30.4375")

# Below a year, annualising extrapolates noise: three months at +8% reads as
# +36%/year. Refused rather than reported.
MIN_DAYS_FOR_A_RATE = 365

# Below three years an annualised figure is statistically weak: labelled, not hidden.
WEAK_RATE_DAYS = 1096

# No asset class sustains this for a decade: flagged, never rewritten.
EXTREME_ANNUAL_RATE = Decimal("0.30")

# Below this many months with operations, a median month of saving is noise.
MIN_MONTHS_FOR_A_SURPLUS = 6

# TWR only neutralises a flow landing on a priced day; any other reads as
# performance, always upward. Below this share of the final value the error is
# rounding, above it the rate cannot be trusted.
MAX_UNALIGNED_FLOW_SHARE = Decimal("0.02")


@dataclass(frozen=True)
class BasisWarning:
    """A reservation about a derived figure: a code, so the front owns the wording,
    and the quantity it hinges on."""

    code: str
    values: dict = field(default_factory=dict)


#: The same reservations in French, for callers that relay prose (the MCP agent).
WARNING_MESSAGES = {
    "no_contribution_found": "Aucun versement identifié dans le journal : projeté sans apport.",
    "insufficient_history": (
        "Historique trop court ({days} j) pour annualiser un rendement : aucun taux n'est déduit."
    ),
    "unaligned_flows": (
        "{share:.0%} des versements tombent sur des jours sans valorisation : ils seraient "
        "comptés comme de la performance. Aucun taux n'est déduit."
    ),
    "weak_annualisation": "Taux annualisé sur {days} j seulement : statistiquement faible.",
    "extreme_rate": (
        "Rendement historique de {annual_rate:.1%} par an : peu susceptible de tenir sur la "
        "durée projetée."
    ),
    "expected_rate_used": (
        "Rendement des placements pris sur le taux attendu saisi, faute d'un an de "
        "relevés : c'est une hypothèse, pas une mesure."
    ),
    "no_statement": (
        "Aucun relevé de solde saisi sur les placements : aucun rendement n'est déduit."
    ),
    "short_cashflow_history": (
        "Seulement {months} mois d'opérations bancaires sur les douze derniers : "
        "l'épargne mensuelle n'est pas déduite, la banque est projetée sans apport."
    ),
    "no_declared_rate": (
        "Aucun taux saisi sur les livrets : la banque est projetée sans rendement."
    ),
}


def describe(warning: BasisWarning) -> str:
    """Render a warning in French; a message missing its values still beats a crash."""
    template = WARNING_MESSAGES.get(warning.code)
    if template is None:
        return warning.code
    try:
        return template.format(**warning.values)
    except (KeyError, ValueError):
        return template


@dataclass
class CategoryBasis:
    """One category's derived assumptions, with how each was obtained."""

    monthly_contribution: Decimal | None = None
    annual_return_rate: Decimal | None = None
    contribution_source: str = "unavailable"
    return_source: str = "unavailable"
    contribution_months: int = 0
    contribution_total: Decimal = Decimal("0")
    return_days: int = 0
    warnings: list[BasisWarning] = field(default_factory=list)


def average_monthly_contribution(flows: dict[datetime.date, Decimal]) -> tuple[Decimal | None, int, Decimal]:
    """Net external flow per month, averaged over the span the flows cover.

    Net, not gross: 500 in and 200 out is saving 300. The span stops at the last
    flow, not today; whether the rhythm still holds is the caller's question.

    Returns:
        (average, months spanned, net total). Average is None when there are no flows.
    """
    if not flows:
        return None, 0, Decimal("0")

    days = (max(flows) - min(flows)).days
    # Flows within one month still make one month of contribution.
    months = max(Decimal(days) / DAYS_PER_MONTH, Decimal("1"))
    total = sum(flows.values(), Decimal("0"))

    return total / months, int(months), total


def _unaligned_flow_share(
    series: list[tuple[datetime.date, Decimal]],
    flows: dict[datetime.date, Decimal],
) -> Decimal:
    """How much of the flow lands on days the series does not price, as a share
    of the final value: one large deposit distorts the rate, a hundred small
    ones may not."""
    if not flows or not series:
        return Decimal("0")

    priced_days = {day for day, _ in series}
    unaligned = sum(
        abs(amount) for day, amount in flows.items() if day not in priced_days
    )
    if not unaligned:
        return Decimal("0")

    final_value = series[-1][1]
    return unaligned / final_value if final_value > 0 else Decimal("1")


def _category_basis(
    series: list[tuple[datetime.date, Decimal]],
    transactions: list,
) -> CategoryBasis:
    """Derive one category's contribution and return from its own history."""
    basis = CategoryBasis()

    # Both measures read the same ledger, so a deposit cannot count for one and
    # not the other.
    flows = stock_external_flows(transactions)
    average, months, total = average_monthly_contribution(flows)
    if average is not None:
        basis.monthly_contribution = average
        basis.contribution_source = "net_external_flows"
        basis.contribution_months = months
        basis.contribution_total = total
    elif transactions:
        # Most likely an imported ledger of buys: saying so stops "no
        # contribution" reading as "you save nothing".
        basis.warnings.append(BasisWarning("no_contribution_found"))

    series = sorted(series, key=lambda point: point[0])
    if len(series) >= 2:
        span_days = (series[-1][0] - series[0][0]).days
        basis.return_days = span_days

        unaligned_share = _unaligned_flow_share(series, flows)

        if span_days < MIN_DAYS_FOR_A_RATE:
            basis.warnings.append(
                BasisWarning("insufficient_history", {"days": span_days})
            )
        elif unaligned_share > MAX_UNALIGNED_FLOW_SHARE:
            basis.warnings.append(
                BasisWarning("unaligned_flows", {"share": float(unaligned_share)})
            )
        else:
            twr = time_weighted_return(series, flows)
            annual = (
                annualize(twr.total_return, span_days)
                if twr.total_return is not None
                else None
            )
            if annual is not None:
                basis.annual_return_rate = annual
                basis.return_source = "annualised_twr"
                if span_days < WEAK_RATE_DAYS:
                    basis.warnings.append(
                        BasisWarning("weak_annualisation", {"days": span_days})
                    )
                if abs(annual) > EXTREME_ANNUAL_RATE:
                    basis.warnings.append(
                        BasisWarning("extreme_rate", {"annual_rate": float(annual)})
                    )

    return basis


def derive_projection_defaults(
    session: Session, user_uuid: str, master_key: str
) -> dict[str, CategoryBasis]:
    """Measure each category's contribution rhythm and realised return.

    BANK's contribution is what stays on the bank accounts each month, read off
    the real cashflow; its return is at most the rates the user entered on
    their savings accounts (see `_bank_basis`).
    """
    from services.crypto_account import get_all_crypto_accounts_history, get_user_crypto_accounts
    from services.crypto_transaction import get_account_transactions as get_crypto_transactions
    from services.stock_account import get_all_stock_accounts_history, get_user_stock_accounts
    from services.stock_transaction import get_account_transactions as get_stock_transactions

    stock_transactions = []
    for account in get_user_stock_accounts(session, user_uuid, master_key):
        stock_transactions.extend(get_stock_transactions(session, account.id, master_key))

    crypto_transactions = []
    for account in get_user_crypto_accounts(session, user_uuid, master_key):
        crypto_transactions.extend(get_crypto_transactions(session, account.id, master_key))

    stock_series = [
        (snapshot.snapshot_date, Decimal(snapshot.total_value))
        for snapshot in get_all_stock_accounts_history(session, user_uuid, master_key)
    ]
    crypto_series = [
        (snapshot.snapshot_date, Decimal(snapshot.total_value))
        for snapshot in get_all_crypto_accounts_history(session, user_uuid, master_key)
    ]

    return {
        "STOCK": _category_basis(stock_series, stock_transactions),
        "CRYPTO": _category_basis(crypto_series, crypto_transactions),
        "BANK": _bank_basis(session, user_uuid, master_key),
        "PLACEMENT": _placements_basis(session, user_uuid, master_key),
    }


def _bank_basis(session: Session, user_uuid: str, master_key: str) -> CategoryBasis:
    """What stays on the bank accounts each month, and what they earn.

    The surplus is income, less expenses, less what left for the investment
    accounts: that money arrives as the other categories' own contributions,
    and subtracting it here keeps a euro from being projected twice. Money set
    aside on a savings account stays in the bank, so it is not subtracted. The
    median month of the last twelve completed, so a bonus or a holiday does not
    set the rhythm; with fewer than six months of operations nothing is deduced.

    The return is the rates the user entered, spread over every bank balance.
    """
    from statistics import median

    from services.banking.real_cashflow import real_cashflow_recent
    from services.savings_interest import declared_bank_rate

    bank = CategoryBasis()
    covered = [
        month for month in real_cashflow_recent(session, user_uuid, master_key, months=12).months
        if month.operation_count
    ]
    if len(covered) >= MIN_MONTHS_FOR_A_SURPLUS:
        surpluses = [month.income - month.expenses - month.investment for month in covered]
        bank.monthly_contribution = Decimal(median(surpluses))
        bank.contribution_source = "real_cashflow"
        bank.contribution_months = len(covered)
        bank.contribution_total = sum(surpluses, Decimal("0"))
    else:
        bank.warnings.append(BasisWarning("short_cashflow_history", {"months": len(covered)}))

    rate = declared_bank_rate(session, user_uuid, master_key)
    if rate is None:
        bank.warnings.append(BasisWarning("no_declared_rate"))
    else:
        bank.annual_return_rate = rate
        bank.return_source = "declared_rates"
    return bank


def _placements_basis(session: Session, user_uuid: str, master_key: str) -> CategoryBasis:
    """Derive contribution and return for placements, their rates weighted by
    value. Without a year of statements, a placement's rate is the one the user
    expects."""
    from services.placement import build_timeline, get_user_placements

    basis = CategoryBasis()
    summary = get_user_placements(session, user_uuid, master_key)
    if not summary.accounts:
        return basis

    flows: dict[datetime.date, Decimal] = {}
    weighted = Decimal("0")
    weight = Decimal("0")
    sources: set[str] = set()
    for placement in summary.accounts:
        timeline = build_timeline(session, placement.id, master_key)
        for day, amount in timeline.flows.items():
            flows[day] = flows.get(day, Decimal("0")) + amount
        basis.return_days = max(basis.return_days, placement.return_days)

        if placement.annual_return_rate is not None:
            rate, source = placement.annual_return_rate, "observed_twr"
        elif placement.expected_return_rate is not None:
            rate, source = placement.expected_return_rate, "expected_rate"
        else:
            continue
        # An empty placement still counts, so its rate is not dropped.
        share = max(placement.current_value, Decimal("1"))
        weighted += rate * share
        weight += share
        sources.add(source)

    # Span up to today so a single lump sum is not projected as a recurring monthly flow.
    if flows:
        today = datetime.date.today()
        flows.setdefault(today, Decimal("0"))
    average, months, total = average_monthly_contribution(flows)
    if average is not None and total != 0:
        basis.monthly_contribution = average
        basis.contribution_source = "net_external_flows"
        basis.contribution_months = months
        basis.contribution_total = total
    else:
        basis.warnings.append(BasisWarning("no_contribution_found"))

    if weight > 0:
        basis.annual_return_rate = weighted / weight
        basis.return_source = "observed_twr" if sources == {"observed_twr"} else "expected_rate"
        if "expected_rate" in sources:
            basis.warnings.append(BasisWarning("expected_rate_used"))
        elif basis.return_days < WEAK_RATE_DAYS:
            basis.warnings.append(BasisWarning("weak_annualisation", {"days": basis.return_days}))
    elif not any(c.last_valuation_date for c in summary.accounts):
        basis.warnings.append(BasisWarning("no_statement"))
    else:
        basis.warnings.append(BasisWarning("insufficient_history", {"days": basis.return_days}))
    return basis
