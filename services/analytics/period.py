"""How each investment pocket did over a window of days.

What a pocket was worth at each end, what was paid into it in between, what it
earned — the rest — and the time-weighted return, which neutralises when the
money arrived. The gain answers "how much did I make"; the return answers "how
well did it do", and a large deposit before a rise inflates the first and not
the second.

The stock and crypto series are the accounts' daily snapshots, idle cash
included, so a deposit shows up as value and as flow alike and never as gain.
"""

import datetime
from dataclasses import dataclass, field
from decimal import Decimal

from sqlmodel import Session

from services.analytics.flows import stock_external_flows
from services.analytics.projection_basis import (
    MAX_UNALIGNED_FLOW_SHARE,
    MIN_DAYS_FOR_A_RATE,
    _unaligned_flow_share,
)
from services.analytics.returns import annualize, time_weighted_return

_ZERO = Decimal("0")


@dataclass
class PocketPeriod:
    """One pocket over the window. None where the pocket has nothing to say."""

    start: datetime.date | None = None
    end: datetime.date | None = None
    value_start: Decimal | None = None
    value_end: Decimal | None = None
    net_contributions: Decimal = _ZERO
    gain: Decimal | None = None
    time_weighted_return: Decimal | None = None
    # Only once the window spans a year: annualising less extrapolates noise.
    annualised_return: Decimal | None = None
    warnings: list[str] = field(default_factory=list)


def _series_period(
    series: list[tuple[datetime.date, Decimal]],
    flows: dict[datetime.date, Decimal],
    start: datetime.date,
    end: datetime.date,
) -> PocketPeriod:
    """Measure a daily value series and its external flows over [start, end].

    The baseline is the last value on or before `start`: a window opening on a
    day without a snapshot still starts from what the pocket held then.
    """
    ordered = sorted(point for point in series if point[0] <= end)
    before = [point for point in ordered if point[0] <= start]
    inside = [point for point in ordered if point[0] > start]
    if not inside and not before:
        return PocketPeriod()

    baseline = before[-1] if before else None
    window = ([baseline] if baseline else []) + inside
    first_day, last_day = window[0][0], window[-1][0]
    window_flows = {day: amount for day, amount in flows.items() if first_day < day <= last_day}
    contributions = sum(window_flows.values(), _ZERO)

    period = PocketPeriod(
        start=first_day,
        end=last_day,
        value_start=window[0][1] if baseline else _ZERO,
        value_end=window[-1][1],
        net_contributions=contributions,
    )
    if not baseline:
        # Opened inside the window: everything paid in since counts as flow,
        # and the first snapshot's own deposit with it.
        opening = {day: amount for day, amount in flows.items() if day <= first_day}
        period.net_contributions += sum(opening.values(), _ZERO)
        period.warnings.append(f"Poche ouverte le {first_day.isoformat()}, dans la période.")
    period.gain = period.value_end - period.value_start - period.net_contributions

    if len(window) < 2:
        return period
    if _unaligned_flow_share(window, window_flows) > MAX_UNALIGNED_FLOW_SHARE:
        period.warnings.append(
            "Des versements tombent sur des jours sans valorisation : le rendement pondéré "
            "n'est pas calculé, le gain reste juste."
        )
        return period
    twr = time_weighted_return(window, window_flows)
    period.time_weighted_return = twr.total_return
    span = (last_day - first_day).days
    if twr.total_return is not None and span >= MIN_DAYS_FOR_A_RATE:
        period.annualised_return = annualize(twr.total_return, span)
    return period


def _placements_period(session: Session, user_uuid: str, master_key: str, start, end) -> PocketPeriod:
    """Placements move only on statements and flows: a gain, never a daily return."""
    from services.placement import build_timeline, get_user_placements

    summary = get_user_placements(session, user_uuid, master_key)
    if not summary.accounts:
        return PocketPeriod()

    value_start = value_end = contributions = _ZERO
    for placement in summary.accounts:
        timeline = build_timeline(session, placement.id, master_key)
        value_start += timeline.value_on(start)
        value_end += timeline.value_on(end)
        contributions += sum(
            (amount for day, amount in timeline.flows.items() if start < day <= end), _ZERO
        )
    return PocketPeriod(
        start=start,
        end=end,
        value_start=value_start,
        value_end=value_end,
        net_contributions=contributions,
        gain=value_end - value_start - contributions,
        warnings=[
            "Valeur tirée des relevés saisis : le gain n'apparaît qu'aux dates de relevé."
        ],
    )


def period_performance(
    session: Session,
    user_uuid: str,
    master_key: str,
    start: datetime.date,
    end: datetime.date | None = None,
) -> dict[str, PocketPeriod]:
    """Each investment pocket's gain and return between `start` and `end` (today by default)."""
    from services.crypto_account import get_all_crypto_accounts_history, get_user_crypto_accounts
    from services.crypto_transaction import get_account_transactions as get_crypto_transactions
    from services.stock_account import get_all_stock_accounts_history, get_user_stock_accounts
    from services.stock_transaction import get_account_transactions as get_stock_transactions

    end = end or datetime.date.today()

    def measure(accounts, read_transactions, read_history) -> PocketPeriod:
        transactions = []
        for account in accounts:
            transactions.extend(read_transactions(session, account.id, master_key))
        series = [
            (snapshot.snapshot_date, Decimal(snapshot.total_value))
            for snapshot in read_history(session, user_uuid, master_key, include_current=True)
        ]
        return _series_period(series, stock_external_flows(transactions), start, end)

    return {
        "stocks": measure(
            get_user_stock_accounts(session, user_uuid, master_key),
            get_stock_transactions,
            get_all_stock_accounts_history,
        ),
        "crypto": measure(
            get_user_crypto_accounts(session, user_uuid, master_key),
            get_crypto_transactions,
            get_all_crypto_accounts_history,
        ),
        "placements": _placements_period(session, user_uuid, master_key, start, end),
    }
