"""Defaults a wealth projection should start from, measured rather than guessed.

A projection is only as honest as the two numbers it assumes: what the investor
puts in each month, and what the portfolio returns. ``services/projection`` has
to invent both when the caller supplies neither, and it does so with a shortcut
that is wrong in a specific, quantifiable way:

    injection = total_invested / months_since_first_transaction
    rate      = (value / invested) ** (1 / years) - 1

``total_invested`` is the cost basis of what is *currently held*, not the money
that came in — rotate a position and it grows without a euro being deposited.
And the rate divides the value by the cost basis, which treats a euro deposited
last week as though it had been compounding since the first day. That is not a
return: it understates a portfolio fed by regular contributions, the more so the
younger the contributions are, and it is exactly the error time-weighting exists
to remove.

This module derives both figures with the measures the rest of the analytics
subsystem already implements:

**Return — annualised TWR.** Time-weighted return chains daily performance with
external flows neutralised, so it measures the portfolio rather than the timing
of the deposits (GIPS' basis for reporting performance, and what a retail app
shows as "performance"). Money-weighted return (XIRR) answers a different
question — how *this investor* did, timing included — which is the right lens on
the past and the wrong one to project forward with, since it would bake a lucky
or unlucky entry sequence into every future month.

**Contribution — real external flows.** Deposits net of withdrawals, straight
from the ledger, averaged over the period they span. Auto-provisions count: the
application writes one when a buy exceeds the cash on hand, which means the
money came from outside the portfolio, whatever the row is called. The
money-weighted return excludes them because their *date* is synthetic and a
solver is sensitive to it; an average over years is not.

Nothing here is capped or smoothed silently. Where a figure is too fragile to
stand on its own — an annualised return from ten months of history, a rate no
portfolio sustains for a decade — it comes back with a warning attached, for the
caller to pass on rather than bury.
"""

import datetime
from dataclasses import dataclass, field
from decimal import Decimal

from sqlmodel import Session

from services.analytics.flows import stock_external_flows
from services.analytics.returns import annualize, time_weighted_return

# 365.25 / 12, so a month means the same thing here as it does in `annualize`.
DAYS_PER_MONTH = Decimal("30.4375")

# Below a year, an annualised return is an extrapolation of noise: three months
# of +8% becomes +36%/year by construction. Refused rather than reported.
MIN_DAYS_FOR_A_RATE = 365

# The analytics spec calls an annualised figure under three years statistically
# weak and requires it be labelled, not hidden. Same threshold, same treatment.
WEAK_RATE_DAYS = 1096

# No asset class sustains this for a decade. Projecting it compounds a bull run
# into a fortune, so it is flagged — but never quietly rewritten.
EXTREME_ANNUAL_RATE = Decimal("0.30")

# Time-weighting only removes a flow that lands on a day the series prices. A
# deposit on a day with no snapshot is read as performance instead, and the
# error runs one way: upward. Tolerated below this share of the final value —
# a stray weekend deposit moves the rate by less than the rounding — and fatal
# above it, because there is no way to tell how much of the return is real.
MAX_UNALIGNED_FLOW_SHARE = Decimal("0.02")


@dataclass(frozen=True)
class BasisWarning:
    """A reservation about a derived figure, as a code plus what it hinges on.

    A code rather than a sentence: the web app writes its own wording, and a
    translation or a rewrite must not depend on matching a string produced by
    the server. The values travel alongside because every one of these
    reservations is about a quantity — "too short" means nothing without the
    number of days it was too short by.
    """

    code: str
    values: dict = field(default_factory=dict)


#: The reservations a derived figure can carry, in French, for a model to relay.
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
    "not_measured": (
        "Aucun rendement ni versement déduit pour la banque : les soldes bougent avec les "
        "revenus et les dépenses, pas avec une performance."
    ),
}


def describe(warning: BasisWarning) -> str:
    """Render a warning in French, for callers that need prose rather than a code."""
    template = WARNING_MESSAGES.get(warning.code)
    if template is None:
        return warning.code
    try:
        return template.format(**warning.values)
    except (KeyError, ValueError):
        # A message missing its values is still worth showing; a crash is not.
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

    Net, not gross: someone who pays 500 in and takes 200 back out is saving 300
    a month, and projecting the gross figure would invent the difference.

    The span runs from the first flow to the last, not to today — a ledger that
    stops six months ago describes a rhythm over the months it actually covers.
    Whether that rhythm still holds is the caller's question to raise, and the
    span is returned so it can.

    Returns:
        (average, months spanned, net total). Average is None when there is
        nothing to average.
    """
    if not flows:
        return None, 0, Decimal("0")

    days = (max(flows) - min(flows)).days
    # A single day, or several inside one month, still represents one month of
    # contribution — dividing by zero days would report an infinite rhythm.
    months = max(Decimal(days) / DAYS_PER_MONTH, Decimal("1"))
    total = sum(flows.values(), Decimal("0"))

    return total / months, int(months), total


def _unaligned_flow_share(
    series: list[tuple[datetime.date, Decimal]],
    flows: dict[datetime.date, Decimal],
) -> Decimal:
    """How much of the flow lands on days the series does not price, in shares.

    Measured against the final value rather than counted, because one large
    deposit outside the series distorts the return and a hundred small ones may
    not. Returns zero when there is nothing to compare against.
    """
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

    # Auto-provisions included: see the module docstring. Both measures read the
    # same ledger, so a deposit cannot count for one and not the other.
    flows = stock_external_flows(transactions)

    average, months, total = average_monthly_contribution(flows)
    if average is not None:
        basis.monthly_contribution = average
        basis.contribution_source = "net_external_flows"
        basis.contribution_months = months
        basis.contribution_total = total
    elif transactions:
        # Holdings but no deposit anywhere: an imported ledger of buys, most
        # likely. Projecting no contribution is what the data supports, and
        # saying so is what stops it reading as "you save nothing".
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
    """
    Measure each category's contribution rhythm and realised return.

    BANK is deliberately left underived on both counts. Its snapshots move with
    salary and spending, so a time-weighted return over them would read a payday
    as performance; and the obvious contribution proxy — the cashflow's monthly
    balance — is the very money that already shows up as deposits into the stock
    and crypto accounts, so adopting it would count the same euro twice. The
    projection service's own conservative default stands instead, and the caller
    is told as much.
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

    bank = CategoryBasis()
    bank.warnings.append(BasisWarning("not_measured"))

    return {
        "STOCK": _category_basis(stock_series, stock_transactions),
        "CRYPTO": _category_basis(crypto_series, crypto_transactions),
        "BANK": bank,
    }
