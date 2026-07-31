"""External EUR cash flows for a stock account — the single definition.

Both the daily snapshot rebuild and the investor analytics need to know how much
money entered or left an account on a given day. Two definitions would drift, and
TWR would silently stop matching the stored history.

Auto-provisions are rows the app writes itself one second before a BUY when cash
is short (see services/stock_transaction.py). They are bookkeeping, not decisions:
they are dated on the purchase, not on the real transfer. Snapshot rebuilding
keeps them because they move the account's cash; the analytics drops them.
"""

from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Iterable

AUTO_PROVISION_NOTE = "Provision automatique"

_ZERO = Decimal("0")


def _to_decimal(value: object) -> Decimal:
    """Best-effort conversion, mirroring account_history._to_decimal.

    Swallowing a bad conversion rather than raising is deliberate: this function
    replaces logic that already behaved that way, and a refactor that promises no
    behaviour change must not introduce a new failure mode.
    """
    if value is None:
        return _ZERO
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:
        return _ZERO


def _tx_type(tx) -> str:
    raw = getattr(tx, "type", None)
    return str(getattr(raw, "value", raw) or "")


def _tx_day(tx) -> date | None:
    executed_at = getattr(tx, "executed_at", None)
    return executed_at.date() if executed_at is not None else None


def is_auto_provision(tx) -> bool:
    """True for a cash row the app generated itself to cover a BUY shortfall."""
    if _tx_type(tx) != "DEPOSIT":
        return False
    if str(getattr(tx, "asset_key", "") or "").upper() != "EUR":
        return False
    return (getattr(tx, "notes", None) or "").strip() == AUTO_PROVISION_NOTE


def _signed_flow(tx, include_auto_provisions: bool) -> Decimal:
    if str(getattr(tx, "asset_key", "") or "").upper() != "EUR":
        return _ZERO
    if not include_auto_provisions and is_auto_provision(tx):
        return _ZERO

    amount = _to_decimal(getattr(tx, "amount", _ZERO))
    match _tx_type(tx):
        case "DEPOSIT":
            return amount
        case "WITHDRAW":
            return -amount
        case _:
            return _ZERO


def stock_external_flow_for_day(
    transactions: Iterable[object],
    day: date,
    *,
    include_auto_provisions: bool = True,
) -> Decimal:
    """Signed external EUR flow for a single day. Positive = money entering."""
    total = _ZERO
    for tx in transactions or ():
        if _tx_day(tx) != day:
            continue
        total += _signed_flow(tx, include_auto_provisions)
    return total


def stock_external_flows(
    transactions: Iterable[object],
    *,
    include_auto_provisions: bool = True,
) -> dict[date, Decimal]:
    """Signed external EUR flow per day. Days with no net flow are omitted."""
    grouped: dict[date, Decimal] = defaultdict(lambda: _ZERO)
    for tx in transactions or ():
        day = _tx_day(tx)
        if day is None:
            continue
        flow = _signed_flow(tx, include_auto_provisions)
        if flow != _ZERO:
            grouped[day] += flow
    return {day: total for day, total in grouped.items() if total != _ZERO}
