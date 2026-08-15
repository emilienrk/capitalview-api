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
import json
from decimal import Decimal

from sqlmodel import Session, select

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
from services.cashflow import get_user_cashflow_balance
from services.crypto_account import get_all_crypto_accounts_history, get_user_crypto_accounts
from services.crypto_transaction import (
    get_account_transactions as get_crypto_transactions,
)
from services.crypto_transaction import (
    get_crypto_account_summary,
)
from services.encryption import decrypt_data, hash_index
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

    all_dates = sorted(
        stock_snaps.keys() | crypto_snaps.keys() | bank_snaps.keys() | assets_snaps.keys()
    )

    history = []
    for day in all_dates:
        stock_v = stock_snaps.get(day, Decimal("0"))
        crypto_v = crypto_snaps.get(day, Decimal("0"))
        bank_v = bank_snaps.get(day, Decimal("0"))
        assets_v = assets_snaps.get(day, Decimal("0"))
        history.append(
            {
                "snapshot_date": day,
                "total_wealth": stock_v + crypto_v + bank_v + assets_v,
                "stock_value": stock_v,
                "crypto_value": crypto_v,
                "bank_value": bank_v,
                "assets_value": assets_v,
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


def _as_position(position) -> dict:
    """A held line with both what it is worth and what it cost.

    Without the cost basis a reader can say how much you hold but not whether
    you are up on it, which is most of what anyone wants to know.
    """
    return {
        "symbol": position.symbol,
        "amount": float(position.total_amount),
        "current_value": float(position.current_value) if position.current_value else 0.0,
        "total_invested": float(position.total_invested),
        "average_buy_price": float(position.average_buy_price),
        "profit_loss": _opt_float(position.profit_loss),
        "profit_loss_percentage": _opt_float(position.profit_loss_percentage),
    }


def get_user_balance(session: Session, user_uuid: str, master_key: bytes, details: bool = False, date: str = None) -> dict:
    user_bidx = hash_index(user_uuid, master_key)
    settings = get_or_create_settings(session, user_uuid, master_key)

    result = {}

    # --- Stock Accounts ---
    stock_models = session.exec(
        select(StockAccount).where(StockAccount.user_uuid_bidx == user_bidx)
    ).all()

    stock_current_value = Decimal(0)
    stock_invested = Decimal(0)
    stock_pnl: list[Decimal] = []
    stock_accounts_details = []
    for acc in stock_models:
        transactions = get_stock_transactions(session, acc.uuid, master_key)
        summary = get_stock_account_summary(session, transactions, as_of=date, db_only=True)
        # Net worth = holdings VALEUR + idle account cash.
        acc_val = (summary.current_value or Decimal(0)) + summary.cash_balance
        stock_current_value += acc_val
        stock_invested += summary.total_invested
        if summary.profit_loss is not None:
            stock_pnl.append(summary.profit_loss)

        if details:
            acc_name = decrypt_data(acc.name_enc, master_key)
            positions = [_as_position(p) for p in summary.positions if p.total_amount != 0]
            stock_accounts_details.append({
                "name": acc_name,
                "total_value": float(acc_val),
                "total_invested": float(summary.total_invested),
                "profit_loss": _opt_float(summary.profit_loss),
                "realized_profit_loss": _opt_float(summary.realized_profit_loss),
                "cash_balance": float(summary.cash_balance),
                "positions": positions
            })

    # --- Crypto Accounts ---
    crypto_models = session.exec(
        select(CryptoAccount).where(CryptoAccount.user_uuid_bidx == user_bidx)
    ).all()

    crypto_current_value = Decimal(0)
    crypto_invested = Decimal(0)
    crypto_pnl: list[Decimal] = []
    crypto_accounts_details = []
    for acc in crypto_models:
        transactions = get_crypto_transactions(session, acc.uuid, master_key)
        summary = get_crypto_account_summary(session, transactions, as_of=date, db_only=True)
        # Net worth = holdings VALEUR + idle account cash.
        acc_val = (summary.current_value or Decimal(0)) + summary.cash_balance
        crypto_current_value += acc_val
        crypto_invested += summary.total_invested
        if summary.profit_loss is not None:
            crypto_pnl.append(summary.profit_loss)

        if details:
            acc_name = decrypt_data(acc.name_enc, master_key)
            positions = [_as_position(p) for p in summary.positions if p.total_amount != 0]
            crypto_accounts_details.append({
                "name": acc_name,
                "total_value": float(acc_val),
                "total_invested": float(summary.total_invested),
                "profit_loss": _opt_float(summary.profit_loss),
                "realized_profit_loss": _opt_float(summary.realized_profit_loss),
                "cash_balance": float(summary.cash_balance),
                "positions": positions
            })

    # --- Bank Accounts (Cash) ---
    cash_total = Decimal(0)
    bank_accounts_details = []
    if settings.bank_module_enabled:
        if date:
            from datetime import datetime
            target_date = datetime.strptime(date, "%Y-%m-%d").date()
            bank_summary = get_all_bank_accounts_snapshot_for_date(session, user_uuid, target_date, master_key)
            if bank_summary:
                cash_total = bank_summary.total_value
                accounts = bank_summary.accounts
            else:
                cash_total = Decimal(0)
                accounts = []

        else:
            bank_summary = get_user_bank_accounts(session, user_uuid, master_key)
            cash_total = bank_summary.total_balance
            accounts = bank_summary.accounts or []

        if details:
            for bank_acc in accounts:
                bank_accounts_details.append({
                    "name": getattr(bank_acc, "name", None),
                    "institution": getattr(bank_acc, "institution_name", None) or getattr(bank_acc, "institution", None),
                    "balance": float(getattr(bank_acc, "balance", 0))
                })

    # --- Real Estate / Other Assets ---
    assets_total = Decimal(0)
    assets_details = []
    if settings.wealth_module_enabled:
        if date:
            from datetime import datetime
            target_date = datetime.strptime(date, "%Y-%m-%d").date()
            asset_summary = get_asset_portfolio_snapshot_for_date(session, user_uuid, target_date, master_key)
            if asset_summary:
                assets_total = asset_summary.total_value
                assets = asset_summary.positions or []
            else:
                assets = []
        else:
            asset_summary = get_user_assets(session, user_uuid, master_key)
            assets_total = asset_summary.total_estimated_value
            assets = asset_summary.assets

        if details:
            for a in assets:
                detail = {
                    "name": getattr(a, "name", None) or getattr(a, "asset_key", None),
                    "estimated_value": float(getattr(a, "estimated_value", 0.0) or getattr(a, "value", 0.0))
                }
                if hasattr(a, "category") and getattr(a, "category"):
                    detail["category"] = a.category
                assets_details.append(detail)

    invested_total = stock_invested + crypto_invested

    # Summed from the accounts rather than derived as value minus cost: the
    # per-type totals above fold in each account's idle cash, so subtracting the
    # cost basis from them would report an untouched cash balance as a gain.
    # None, not zero, when no account could be priced — zero would read as flat.
    priced = stock_pnl + crypto_pnl
    unrealized = sum(priced) if priced else None

    result.update({
        "stocks_total": float(stock_current_value),
        "crypto_total": float(crypto_current_value),
        "cash_total": float(cash_total),
        "assets_total": float(assets_total),
        "global_wealth": float(stock_current_value + crypto_current_value + cash_total + assets_total),
        # Cost basis and the gain it implies. Without these a reader knows the
        # size of the portfolio but not whether it has made or lost money.
        "stocks_invested": float(stock_invested),
        "crypto_invested": float(crypto_invested),
        "invested_total": float(invested_total),
        "unrealized_profit_loss": _opt_float(unrealized),
    })

    if details:
        result.update({
            "stock_accounts_details": stock_accounts_details,
            "crypto_accounts_details": crypto_accounts_details,
            "bank_accounts_details": bank_accounts_details,
            "assets_details": assets_details
        })

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

def get_user_cashflow(session: Session, user_uuid: str, master_key: bytes, details: bool = False, flow_type: str = None) -> dict:
    parsed_flow = None
    if flow_type:
        try:
            parsed_flow = FlowType(flow_type.lower())
        except ValueError:
            pass

    output = dict()
    balance = get_user_cashflow_balance(session, user_uuid, master_key)
    if details:
        return json.loads(balance.model_dump_json())
    if parsed_flow == FlowType.INFLOW or parsed_flow is None:
        output["inflow"] = { "total": float(balance.total_inflows), "monthly_inflows": float(balance.monthly_inflows) }
    if parsed_flow == FlowType.OUTFLOW or parsed_flow is None:
        output["outflow"] = { "total": float(balance.total_outflows), "monthly_outflows": float(balance.monthly_outflows) }
    if parsed_flow is None:
        output["balance"] = float(balance.net_balance)
        output["monthly_balance"] = float(balance.monthly_balance)
        output["savings_rate"] = float(balance.savings_rate) if balance.savings_rate else None
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
