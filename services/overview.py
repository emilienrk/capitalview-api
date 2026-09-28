"""Cross-domain read models: the composed answers about a user's money.

These functions compose several domain services into one coherent picture —
net worth across every account type, performance over a period, the budget
balance. Nothing about them is specific to any one caller.

That neutrality is the point. Three consumers read from here as peers:

    services/ai/agents/     the in-app assistant, via services/ai/tools.py
    mcp_server/tools.py     agent clients over MCP
    routes/dashboard.py     the web app's greeting card

None of them owns this module. Changing a return shape for one changes it for
all three at once, and no test will tell you — so make the change deliberately,
and check the other two still read correctly.

This module lived in ``services/ai/tools.py`` until the MCP server became its
third consumer, which made an AI-specific home indefensible for logic that was
never AI-specific.
"""

import datetime
from dataclasses import dataclass, field
from decimal import Decimal

from sqlmodel import Session, select

from dtos.crypto import FIAT_ASSET_KEYS
from models import CryptoAccount, StockAccount
from models.enums import FlowType
from services.asset import (
    get_asset_portfolio_history,
    get_asset_portfolio_snapshot_for_date,
    get_user_assets,
)
from services.bank import (
    get_all_bank_accounts_history,
    get_all_bank_accounts_snapshot_for_date,
    get_user_bank_accounts,
)
from services.banking.real_cashflow import stale_balances
from services.cashflow import get_user_cashflow_balance
from services.crypto_account import get_all_crypto_accounts_history, get_user_crypto_accounts
from services.crypto_transaction import (
    get_account_transactions as get_crypto_transactions,
)
from services.crypto_transaction import (
    get_crypto_account_summary,
)
from services.encryption import decrypt_data, hash_index
from services.market import latest_price_dates
from services.placement import build_timeline, get_all_placements_history, get_user_placements
from services.settings import get_or_create_settings
from services.stock_account import get_all_stock_accounts_history, get_user_stock_accounts
from services.stock_transaction import (
    get_account_transactions as get_stock_transactions,
)
from services.stock_transaction import (
    get_stock_account_summary,
)


def build_wealth_history(session: Session, user_uuid: str, master_key: str) -> list[dict]:
    """
    One entry per day: total wealth and how it split across account types.

    The union of every category's snapshot dates, so a day where only the bank
    moved still appears. A category with no snapshot on a given day contributes
    zero rather than being omitted — the caller charts a stacked total and needs
    every series to line up.

    Values stay Decimal; rounding and serialisation belong to the caller.
    """
    settings = get_or_create_settings(session, user_uuid, master_key)

    stock_snaps = {
        s.snapshot_date: s.total_value
        for s in get_all_stock_accounts_history(session, user_uuid, master_key, include_current=False)
    }
    crypto_snaps = {
        s.snapshot_date: s.total_value
        for s in get_all_crypto_accounts_history(session, user_uuid, master_key, include_current=False)
    }

    bank_snaps: dict = {}
    if settings.bank_module_enabled:
        bank_snaps = {
            s.snapshot_date: s.total_value
            for s in get_all_bank_accounts_history(session, user_uuid, master_key)
        }

    assets_snaps: dict = {}
    if settings.wealth_module_enabled:
        assets_snaps = {
            s.snapshot_date: s.total_value
            for s in get_asset_portfolio_history(session, user_uuid, master_key)
        }

    placements_snaps = {
        s.snapshot_date: s.total_value
        for s in get_all_placements_history(session, user_uuid, master_key)
    }

    all_dates = sorted(
        stock_snaps.keys()
        | crypto_snaps.keys()
        | bank_snaps.keys()
        | assets_snaps.keys()
        | placements_snaps.keys()
    )

    history = []
    for day in all_dates:
        stock_v = stock_snaps.get(day, Decimal("0"))
        crypto_v = crypto_snaps.get(day, Decimal("0"))
        bank_v = bank_snaps.get(day, Decimal("0"))
        assets_v = assets_snaps.get(day, Decimal("0"))
        placements_v = placements_snaps.get(day, Decimal("0"))
        history.append(
            {
                "snapshot_date": day,
                "total_wealth": stock_v + crypto_v + bank_v + assets_v + placements_v,
                "stock_value": stock_v,
                "crypto_value": crypto_v,
                "bank_value": bank_v,
                "assets_value": assets_v,
                "placements_value": placements_v,
            }
        )

    return history


def list_transactions(
    session: Session,
    user_uuid: str,
    master_key: str,
    account_type: str = "all",
    since: datetime.date | None = None,
    until: datetime.date | None = None,
    limit: int | None = None,
) -> list[dict]:
    """
    The user's buy/sell movements across accounts, newest first.

    Transactions are stored per account and encrypted, so there is no query that
    filters them in the database — every account is read and decrypted, then
    filtered here. Callers should pass a window rather than asking for a whole
    ledger.

    Args:
        account_type: "stock", "crypto" or "all"
        since/until: inclusive bounds on the execution date
        limit: keep only the *limit* most recent, applied after filtering
    """
    collected: list[dict] = []

    if account_type in ("all", "stock"):
        for account in get_user_stock_accounts(session, user_uuid, master_key):
            for tx in get_stock_transactions(session, account.id, master_key):
                collected.append(_as_movement(tx, "stock", account.name))

    if account_type in ("all", "crypto"):
        for account in get_user_crypto_accounts(session, user_uuid, master_key):
            for tx in get_crypto_transactions(session, account.id, master_key):
                collected.append(_as_movement(tx, "crypto", account.name))

    if since:
        collected = [m for m in collected if m["executed_at"].date() >= since]
    if until:
        collected = [m for m in collected if m["executed_at"].date() <= until]

    collected.sort(key=lambda m: m["executed_at"], reverse=True)

    return collected[:limit] if limit else collected


def _as_movement(transaction, account_type: str, account_name: str) -> dict:
    """Flatten a transaction into the fields that describe what happened."""
    return {
        "account_type": account_type,
        "account_name": account_name,
        "asset_key": transaction.asset_key,
        "type": transaction.type,
        "amount": transaction.amount,
        "price_per_unit": transaction.price_per_unit,
        "total_cost": transaction.total_cost,
        "fees": transaction.fees,
        "currency": transaction.currency,
        "executed_at": transaction.executed_at,
    }


def _opt_float(value: Decimal | None) -> float | None:
    """Keep an absent figure absent — 0.0 would read as 'flat', which is a lie."""
    return float(value) if value is not None else None


def _pct(part: Decimal | None, whole: Decimal | None) -> float | None:
    """*part* as a percentage of *whole*, None when either is missing or whole is not positive."""
    if part is None or whole is None or whole <= 0:
        return None
    return round(float(part / whole * 100), 2)


@dataclass
class _Pocket:
    """A stock or crypto pocket: what its lines are worth, the cash idle beside
    them, and what they cost."""
    holdings: Decimal = Decimal(0)
    cash: Decimal = Decimal(0)
    invested: Decimal = Decimal(0)
    realized: Decimal = Decimal(0)
    dividends: Decimal = Decimal(0)
    fees: Decimal = Decimal(0)
    # One entry per account that could be priced.
    priced_profit_loss: list[Decimal] = field(default_factory=list)
    # (account detail, its held lines), completed once the pocket's total is known.
    accounts: list[tuple[dict, list]] = field(default_factory=list)
    held_keys: set[str] = field(default_factory=set)

    @property
    def profit_loss(self) -> Decimal | None:
        # Summed from the accounts rather than derived as value minus cost, and
        # None rather than zero when no account could be priced: zero reads as flat.
        return sum(self.priced_profit_loss) if self.priced_profit_loss else None

    def summary(self, total: Decimal) -> dict:
        return {
            "value": float(self.holdings),
            "invested": float(self.invested),
            "profit_loss": _opt_float(self.profit_loss),
            "profit_loss_pct": _pct(self.profit_loss, self.invested),
            "realized_profit_loss": float(self.realized),
            "dividends": float(self.dividends),
            "fees": float(self.fees),
            "share_pct": _pct(self.holdings, total),
        }

    def account_details(self) -> list[dict]:
        return [
            {**detail, "positions": [_as_position(line, self.holdings) for line in lines]}
            for detail, lines in self.accounts
        ]


def _as_position(position, pocket_holdings: Decimal) -> dict:
    """A held line with both what it is worth and what it cost.

    Without the cost basis a reader can say how much you hold but not whether
    you are up on it, which is most of what anyone wants to know. The name goes
    with the ticker: "PUST.PA" tells a reader nothing about what is held.
    """
    return {
        "symbol": position.symbol,
        "name": (position.name or "").strip() or None,
        "quantity": float(position.total_amount),
        "value": _opt_float(position.current_value),
        "invested": float(position.total_invested),
        "average_buy_price": float(position.average_buy_price),
        "profit_loss": _opt_float(position.profit_loss),
        "profit_loss_pct": _opt_float(position.profit_loss_percentage),
        # Of every line in the pocket, across its accounts.
        "weight_pct": _pct(position.current_value, pocket_holdings),
    }


def _read_pocket(
    session: Session,
    models: list,
    master_key: str,
    read_transactions,
    summarise,
    as_of: datetime.date | None,
    details: bool,
    account_type=None,
) -> _Pocket:
    pocket = _Pocket()
    for account in models:
        summary = summarise(session, read_transactions(session, account.uuid, master_key), as_of=as_of, db_only=True)
        holdings = summary.current_value or Decimal(0)
        pocket.holdings += holdings
        pocket.cash += summary.cash_balance
        pocket.invested += summary.total_invested
        pocket.realized += summary.realized_profit_loss or Decimal(0)
        pocket.dividends += summary.total_dividends or Decimal(0)
        pocket.fees += summary.total_fees or Decimal(0)
        if summary.profit_loss is not None:
            pocket.priced_profit_loss.append(summary.profit_loss)

        # The idle cash is the account's `cash`, never a line: listed as both,
        # it was counted twice by whoever added the lines up.
        lines = [
            p for p in summary.positions
            if p.total_amount != 0 and p.asset_key not in FIAT_ASSET_KEYS
        ]
        pocket.held_keys.update(p.asset_key for p in lines)
        if details:
            detail = {"name": decrypt_data(account.name_enc, master_key)}
            if account_type is not None:
                detail["type"] = account_type(account)
            detail.update({
                "value": float(holdings),
                "cash": float(summary.cash_balance),
                "invested": float(summary.total_invested),
                "profit_loss": _opt_float(summary.profit_loss),
                "profit_loss_pct": _opt_float(summary.profit_loss_percentage),
                "realized_profit_loss": _opt_float(summary.realized_profit_loss),
                "dividends": float(summary.total_dividends or 0),
                "fees": float(summary.total_fees or 0),
            })
            pocket.accounts.append((detail, lines))
    return pocket


def _read_bank(session: Session, user_uuid: str, master_key: str, as_of, details: bool, stale: dict[str, str]):
    """The bank balances, today's or as of a past day, and their detail."""
    if as_of:
        snapshot = get_all_bank_accounts_snapshot_for_date(session, user_uuid, as_of, master_key)
        accounts = [
            {"name": a["name"], "institution": a["institution"], "balance": float(a["balance"] or 0)}
            for a in snapshot.get("accounts") or []
        ]
        return snapshot.get("total_value") or Decimal(0), accounts if details else []

    summary = get_user_bank_accounts(session, user_uuid, master_key)
    accounts = []
    if details:
        for account in summary.accounts:
            detail = {
                "name": account.name,
                "institution": account.institution_name,
                "type": account.account_type.value,
                "balance": float(account.balance),
            }
            if account.currency != "EUR":
                detail["currency"] = account.currency
            if account.interest_rate is not None:
                detail["interest_rate_pct"] = float(account.interest_rate)
            if account.id in stale:
                detail["balance_may_be_outdated"] = True
            accounts.append(detail)
    # None when a held currency has no published rate: zero rather than a
    # TypeError, the bank page being where that gap is shown for what it is.
    return summary.total_balance or Decimal(0), accounts


def _read_assets(session: Session, user_uuid: str, master_key: str, as_of, details: bool):
    """The possessions' estimated value, today's or as of a past day."""
    if as_of:
        snapshot = get_asset_portfolio_snapshot_for_date(session, user_uuid, as_of, master_key)
        if not snapshot:
            return Decimal(0), []
        accounts = [{"name": p.asset_key, "value": float(p.value)} for p in snapshot.positions or []]
        return snapshot.total_value, accounts if details else []

    summary = get_user_assets(session, user_uuid, master_key)
    assets = [
        {
            "name": asset.name,
            "category": asset.category,
            "value": float(asset.estimated_value),
            "purchase_price": _opt_float(asset.purchase_price),
            "acquisition_date": asset.acquisition_date,
            "gain": _opt_float(asset.profit_loss),
        }
        for asset in summary.assets
    ] if details else []
    return summary.total_estimated_value, assets


def _read_placements(session: Session, user_uuid: str, master_key: str, as_of, details: bool):
    """The placements' value and net amount paid in, today's or as of a past day."""
    summary = get_user_placements(session, user_uuid, master_key)
    if not as_of:
        accounts = [
            {
                "name": placement.name,
                "type": placement.placement_type.value,
                "value": float(placement.current_value),
                "net_invested": float(placement.net_invested),
                "gain": _opt_float(placement.gain),
                "gain_pct": _opt_float(placement.gain_percentage),
                # The value is the last statement plus the flows since: say how
                # old that statement is rather than let it pass for today's.
                "last_valuation_date": (
                    placement.last_valuation_date.isoformat() if placement.last_valuation_date else None
                ),
            }
            for placement in summary.accounts
        ] if details else []
        gains = [p.gain for p in summary.accounts if p.gain is not None]
        return summary.total_value, summary.net_invested, (sum(gains) if gains else None), accounts

    value = invested = Decimal(0)
    accounts = []
    for placement in summary.accounts:
        timeline = build_timeline(session, placement.id, master_key)
        worth = timeline.value_on(as_of)
        paid_in = timeline.deposits_until(as_of) - timeline.withdrawals_until(as_of)
        value += worth
        invested += paid_in
        if details:
            accounts.append({
                "name": placement.name,
                "type": placement.placement_type.value,
                "value": float(worth),
                "net_invested": float(paid_in),
            })
    return value, invested, None, accounts


# The history's name for each pocket a reference snapshot must carry.
_HISTORY_POCKETS = {
    "bank": "bank_value",
    "stocks": "stock_value",
    "crypto": "crypto_value",
    "placements": "placements_value",
    "assets": "assets_value",
}
# Within a cent of zero a pocket is rounding noise, not money held.
_HELD_EPSILON = Decimal("0.005")


def net_worth_changes(
    history: list[dict], total: Decimal, held: list[str], today: datetime.date
) -> list[dict]:
    """The total against the last snapshot, the start of the month and the start of the year.

    A day's snapshot is the union of every pocket's own snapshots, and a pocket
    with none that day counts zero: measured from such a day, the missing pocket
    would read as a gain. So a reference must carry every pocket held today —
    the rule the dashboard applies. Two references on the same snapshot say the
    same thing twice, so the wider period is dropped.

    Deposits are part of the change: this is how the total moved, not a return.
    """
    columns = [_HISTORY_POCKETS[pocket] for pocket in held]
    candidates = (
        ("last_snapshot", today),
        ("month_start", today.replace(day=1)),
        ("year_start", today.replace(month=1, day=1)),
    )
    changes: list[dict] = []
    seen: set[datetime.date] = set()
    for key, before in candidates:
        reference = next(
            (
                snapshot for snapshot in reversed(history)
                if snapshot["snapshot_date"] < before and all(snapshot[c] > 0 for c in columns)
            ),
            None,
        )
        if reference is None or reference["snapshot_date"] in seen:
            continue
        seen.add(reference["snapshot_date"])
        base = Decimal(reference["total_wealth"])
        changes.append({
            "reference": key,
            "since": reference["snapshot_date"].isoformat(),
            "change": round(float(total - base), 2),
            "change_pct": _pct(total - base, base),
        })
    return changes


def get_user_balance(session: Session, user_uuid: str, master_key: bytes, details: bool = False, date: str = None) -> dict:
    """
    The whole net worth by pocket, with what each pocket cost.

    The pockets follow the dashboard's legend, so a figure a reader quotes is
    the one the user sees: stock and crypto *lines*, the cash idle on those
    accounts apart as broker cash (negative on an overdrawn account), bank
    balances, placements and possessions. Every share is of the global total.

    Undated, it also says how the total moved lately and how fresh its inputs
    are. Dated, it rebuilds the pockets as of that day.
    """
    user_bidx = hash_index(user_uuid, master_key)
    settings = get_or_create_settings(session, user_uuid, master_key)
    # Parsed once: the account summaries compare it against dates, and the
    # string they used to receive made every dated call raise.
    target_date = datetime.date.fromisoformat(date) if date else None
    today = datetime.date.today()

    stocks = _read_pocket(
        session,
        session.exec(select(StockAccount).where(StockAccount.user_uuid_bidx == user_bidx)).all(),
        master_key, get_stock_transactions, get_stock_account_summary, target_date, details,
        account_type=lambda account: decrypt_data(account.account_type_enc, master_key),
    )
    crypto = _read_pocket(
        session,
        session.exec(select(CryptoAccount).where(CryptoAccount.user_uuid_bidx == user_bidx)).all(),
        master_key, get_crypto_transactions, get_crypto_account_summary, target_date, details,
    )

    stale = {} if target_date else stale_balances(session, user_uuid, master_key, today)
    bank_total, bank_accounts = Decimal(0), []
    if settings.bank_module_enabled:
        bank_total, bank_accounts = _read_bank(session, user_uuid, master_key, target_date, details, stale)

    assets_total, assets = Decimal(0), []
    if settings.wealth_module_enabled:
        assets_total, assets = _read_assets(session, user_uuid, master_key, target_date, details)

    placements_total, placements_invested, placements_gain, placements = _read_placements(
        session, user_uuid, master_key, target_date, details
    )

    broker_cash = stocks.cash + crypto.cash
    total = stocks.holdings + crypto.holdings + broker_cash + bank_total + placements_total + assets_total

    result = {
        "as_of": (target_date or today).isoformat(),
        "global_wealth": float(total),
        "pockets": {
            "bank": {"value": float(bank_total), "share_pct": _pct(bank_total, total)},
            "stocks": stocks.summary(total),
            "crypto": crypto.summary(total),
            "broker_cash": {"value": float(broker_cash), "share_pct": _pct(broker_cash, total)},
            "placements": {
                "value": float(placements_total),
                "net_invested": float(placements_invested),
                "gain": _opt_float(placements_gain),
                "share_pct": _pct(placements_total, total),
            },
            "assets": {"value": float(assets_total), "share_pct": _pct(assets_total, total)},
        },
    }

    # Cost basis and the gain it implies, over the stock and crypto lines.
    # Without these a reader knows the size of the portfolio but not whether it
    # has made or lost money.
    priced = stocks.priced_profit_loss + crypto.priced_profit_loss
    result["unrealized_profit_loss"] = _opt_float(sum(priced) if priced else None)

    if not target_date:
        values = {
            "bank": bank_total, "stocks": stocks.holdings, "crypto": crypto.holdings,
            "placements": placements_total, "assets": assets_total,
        }
        held = [pocket for pocket, value in values.items() if abs(value) > _HELD_EPSILON]
        result["changes"] = net_worth_changes(
            build_wealth_history(session, user_uuid, master_key), total, held, today
        )
        price_dates = latest_price_dates(session, stocks.held_keys | crypto.held_keys)
        result["freshness"] = {
            # The stalest of the prices the lines are valued at.
            "prices_as_of": min(price_dates.values()).isoformat() if price_dates else None,
            "stale_bank_accounts": sorted(stale.values()) if settings.bank_module_enabled else [],
        }

    if details:
        result["accounts"] = {
            "stocks": stocks.account_details(),
            "crypto": crypto.account_details(),
            "bank": bank_accounts,
            "placements": placements,
            "assets": assets,
        }

    return result


def get_historical_performance(session: Session, user_uuid: str, master_key: bytes, days: int = 10, account_type: str = "all") -> dict :
    today = datetime.date.today()
    start_date = today - datetime.timedelta(days=days)

    def calculate_metrics(history):
        if not history:
            return {
                "cumulative_pnl_period": 0.0,
                "average_daily_pnl": 0.0,
                "current_value": 0.0,
            }

        first = history[0]
        last = history[-1]

        first_pnl = float(first.cumulative_pnl) if first.cumulative_pnl is not None else 0.0
        last_pnl = float(last.cumulative_pnl) if last.cumulative_pnl is not None else 0.0

        period_pnl = last_pnl - first_pnl
        days_count = len(history)

        return {
            "cumulative_pnl_period": round(period_pnl, 2),
            "average_daily_pnl": round(period_pnl / days_count, 2) if days_count > 0 else 0.0,
            "current_value": float(last.total_value),
        }

    output = dict()
    if account_type == 'all' or account_type == 'stock':
        output["stock"] = calculate_metrics(get_all_stock_accounts_history(session, user_uuid, master_key, include_current=True, start_date=start_date))
    if account_type == 'all' or account_type == 'crypto':
        output["crypto"] = calculate_metrics(get_all_crypto_accounts_history(session, user_uuid, master_key, include_current=True, start_date=start_date))
    return output

def _euros(total: Decimal | None) -> float | None:
    """A total is None when a currency in play has no published rate; it
    travels as null rather than as a made-up number."""
    return round(float(total), 2) if total is not None else None


def _declared_category(category) -> dict:
    items = []
    for flow in category.items:
        item = {
            "name": flow.name,
            "amount": float(flow.amount),
            "frequency": flow.frequency.value if hasattr(flow.frequency, "value") else flow.frequency,
            "monthly_eur": _euros(flow.monthly_amount_eur),
        }
        if flow.currency != "EUR":
            item["currency"] = flow.currency
        if not flow.is_active:
            item["active"] = False
        items.append(item)
    return {"category": category.category, "monthly": _euros(category.monthly_total), "items": items}


def get_user_cashflow(session: Session, user_uuid: str, master_key: bytes, details: bool = False, flow_type: str = None) -> dict:
    """
    The budget the user *declared*: the income and expenses they entered,
    each brought to a month and to euros.

    What actually moved on the bank accounts is the real cashflow
    (``services/banking/real_cashflow``), never this. Only monthly figures are
    given: a plain sum of the amounts would add a yearly bill to a monthly
    salary.
    """
    parsed_flow = None
    if flow_type:
        try:
            # Upper, not lower: FlowType's values are "INFLOW"/"OUTFLOW", while
            # both callers speak lowercase (services/ai/tools.py,
            # mcp_server/tools.py). Lowercasing raised on every single call, so
            # the filter silently did nothing and "only my spending" answered
            # with everything.
            parsed_flow = FlowType(flow_type.upper())
        except ValueError:
            pass

    balance = get_user_cashflow_balance(session, user_uuid, master_key)
    sides = (
        (FlowType.INFLOW, "inflow", balance.monthly_inflows, balance.inflows),
        (FlowType.OUTFLOW, "outflow", balance.monthly_outflows, balance.outflows),
    )

    output = {}
    for flow, key, monthly, summary in sides:
        if parsed_flow not in (None, flow):
            continue
        side = {"monthly": _euros(monthly)}
        if details:
            side["categories"] = [_declared_category(category) for category in summary.categories]
        output[key] = side
    if parsed_flow is None:
        output["monthly_balance"] = _euros(balance.monthly_balance)
        output["savings_rate_pct"] = _euros(balance.savings_rate)
    return output


def build_projection(
    session: Session,
    user_uuid: str,
    master_key: str,
    months: int,
    monthly_stock: float | None = None,
    monthly_crypto: float | None = None,
    monthly_bank: float | None = None,
    annual_return_stock: float | None = None,
    annual_return_crypto: float | None = None,
    annual_return_bank: float | None = None,
    monthly_placements: float | None = None,
    annual_return_placements: float | None = None,
):
    """
    Project the wealth forward from measured assumptions the caller can override.

    Every parameter left at None is filled from
    ``services/analytics/projection_basis``: the monthly contribution from the
    account's real external flows, the return from its annualised time-weighted
    return. Those are the figures a performance report would quote, rather than
    the cost-basis shortcut ``services/projection`` falls back on — see that
    module's docstring for why the shortcut is wrong rather than merely rough.

    A derived figure that would not stand up is not substituted: under a year of
    history yields no rate at all, and the projection then runs flat for that
    category rather than compounding an extrapolation. BANK stays on the
    service's own conservative default by design.

    Returns:
        The service's ``ProjectionResponse`` — it now carries the measurement
        behind each default in ``parameters_used``, so a caller can state what it
        assumed instead of presenting the curve as a forecast.
    """
    from dtos.projection import ProjectionAssetParameters, ProjectionParameters
    from models.enums import AccountCategory
    from models.user import User
    from services.analytics.projection_basis import derive_projection_defaults
    from services.projection import generate_wealth_projection

    user = session.get(User, user_uuid)
    if user is None:
        raise ValueError("Utilisateur introuvable.")

    basis = derive_projection_defaults(session, user_uuid, master_key)

    # Only what the caller asked for: every unset figure falls through to the
    # measured default the projection service now applies on its own, so the web
    # app and an agent client project from the same numbers.
    overrides = {
        AccountCategory.STOCK: (monthly_stock, annual_return_stock),
        AccountCategory.CRYPTO: (monthly_crypto, annual_return_crypto),
        AccountCategory.BANK: (monthly_bank, annual_return_bank),
        AccountCategory.PLACEMENT: (monthly_placements, annual_return_placements),
    }
    assets = {
        category: ProjectionAssetParameters(monthly_injection=contribution, return_rate=rate)
        for category, (contribution, rate) in overrides.items()
        if contribution is not None or rate is not None
    }

    return generate_wealth_projection(
        session,
        user,
        master_key,
        ProjectionParameters(months_to_project=months, assets=assets),
        basis=basis,
    )


def get_performance_since_last_login(session: Session, user_uuid: str, master_key: bytes) -> dict:
    from models.user import User

    user = session.get(User, user_uuid)

    days_since_login = 7
    if user and user.last_login:
        delta = datetime.date.today() - user.last_login.date()
        if delta.days > 0:
            days_since_login = delta.days

    start_date = datetime.date.today() - datetime.timedelta(days=max(1, days_since_login))

    def calc_variation(history):
        if not history:
            return {"absolute_change": 0.0, "relative_change": 0.0, "current_value": 0.0}
        first = history[0]
        last = history[-1]
        first_val = float(first.total_value)
        last_val = float(last.total_value)
        abs_change = last_val - first_val
        rel_change = (abs_change / first_val * 100) if first_val > 0 else 0.0
        return {
            "absolute_change": round(abs_change, 2),
            "relative_change": round(rel_change, 2),
            "current_value": round(last_val, 2)
        }

    stock_history = get_all_stock_accounts_history(session, user_uuid, master_key, include_current=True, start_date=start_date)
    crypto_history = get_all_crypto_accounts_history(session, user_uuid, master_key, include_current=True, start_date=start_date)

    stock_var = calc_variation(stock_history)
    crypto_var = calc_variation(crypto_history)

    total_abs = stock_var["absolute_change"] + crypto_var["absolute_change"]
    total_current = stock_var["current_value"] + crypto_var["current_value"]
    total_first = total_current - total_abs
    total_rel = (total_abs / total_first * 100) if total_first > 0 else 0.0

    return {
        "period_days": days_since_login,
        "is_significant": abs(total_abs) >= 300 or abs(total_rel) >= 2.0,
        "total_absolute_change_eur": round(total_abs, 2),
        "total_relative_change_pct": round(total_rel, 2),
        "stock": stock_var,
        "crypto": crypto_var
    }


def get_user_statistics(session: Session, user_uuid: str, master_key: bytes):
    # TODO : implement monthly deposits, withdrawals, number of transactions, number of positions, history pnl, by account type etc...
    pass
