"""Every line the user has traded, for the pickers on the settings page.

The analysis settings used to ask for ISINs, typed by hand. That is the wrong
question to put to a human: nobody recognises IE00B4L5Y983, and a typo produces
no error at all — the plan simply scores an allocation against a line that does
not exist, and the drift table shows a target nobody holds.

So the choice is made from a list instead. It covers assets sold as well as
assets held: a target allocation is a statement about the future, and "I am
winding this line down to zero" is a legitimate thing to write.

Deliberately cheap. No prices, no window, no market calls — this is a form
helper that has to answer instantly and keep working on a portfolio too young
for the analysis itself to say anything.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlmodel import Session

from services.analytics.labels import label_of, resolve_asset_labels

_ZERO = Decimal("0")
_BUY = "BUY"
_SELL = "SELL"
_EUR = "EUR"


@dataclass(frozen=True)
class AnalysedAsset:
    asset_key: str
    held: bool
    invested_eur: Decimal
    first_bought: date
    last_activity: date


def _dec(value) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except (TypeError, ValueError):
        return _ZERO


def _tx_type(tx) -> str:
    raw = getattr(tx, "type", None)
    return str(getattr(raw, "value", raw) or "")


def _tx_day(tx) -> date | None:
    executed_at = getattr(tx, "executed_at", None)
    return executed_at.date() if executed_at is not None else None


def traded_assets(transactions) -> list[AnalysedAsset]:
    """Assets bought at least once, most heavily invested first.

    A line only ever sold is not returned: without a purchase there is no cost
    to rank it by, and it cannot be part of a plan that was never started.
    """
    quantity: dict[str, Decimal] = {}
    invested: dict[str, Decimal] = {}
    first: dict[str, date] = {}
    last: dict[str, date] = {}

    for tx in transactions or ():
        key = str(getattr(tx, "asset_key", "") or "").upper()
        # Cash rows are the account's balance, not a position to allocate to.
        if not key or key == _EUR:
            continue
        day = _tx_day(tx)
        if day is None:
            continue
        kind = _tx_type(tx)
        if kind not in (_BUY, _SELL):
            continue

        amount = _dec(getattr(tx, "amount", None))
        price = _dec(getattr(tx, "price_per_unit", None))
        if kind == _BUY:
            quantity[key] = quantity.get(key, _ZERO) + amount
            invested[key] = invested.get(key, _ZERO) + amount * price
            first[key] = min(first[key], day) if key in first else day
        else:
            quantity[key] = quantity.get(key, _ZERO) - amount
        last[key] = max(last[key], day) if key in last else day

    rows = [
        AnalysedAsset(
            asset_key=key,
            # A rounding crumb left by a sale is not a position: the portfolio
            # weights drop it too, and the two views must agree.
            held=quantity.get(key, _ZERO) > _ZERO,
            invested_eur=invested.get(key, _ZERO),
            first_bought=first_bought,
            last_activity=last.get(key, first_bought),
        )
        for key, first_bought in first.items()
    ]
    # Held lines first, then by how much went into them: the line someone is
    # looking for is almost always one of their biggest.
    rows.sort(key=lambda row: (not row.held, -row.invested_eur, row.asset_key))
    return rows


def build_asset_universe(session: Session, transactions) -> list[dict]:
    """The traded lines, labelled, in the shape the settings pickers read."""
    rows = traded_assets(transactions)
    labels = resolve_asset_labels(session, [row.asset_key for row in rows])
    return [
        {
            **label_of(labels, row.asset_key).as_dict(),
            "held": row.held,
            "invested_eur": round(row.invested_eur, 2),
            "first_bought": row.first_bought,
            "last_activity": row.last_activity,
        }
        for row in rows
    ]
