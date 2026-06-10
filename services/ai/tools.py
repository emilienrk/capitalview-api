import datetime
import json
from decimal import Decimal
from typing import Any, Callable
from sqlmodel import Session, select

from models.enums import FlowType
from models import StockAccount, CryptoAccount
from services.encryption import hash_index, decrypt_data
from services.settings import get_or_create_settings
from services.bank import get_user_bank_accounts, get_all_bank_accounts_snapshot_for_date
from services.asset import get_user_assets, get_asset_portfolio_snapshot_for_date
from services.stock_transaction import get_stock_account_summary, get_account_transactions as get_stock_transactions
from services.crypto_transaction import get_crypto_account_summary, get_account_transactions as get_crypto_transactions
from services.cashflow import get_user_cashflow_balance
from services.crypto_account import get_all_crypto_accounts_history
from services.stock_account import get_all_stock_accounts_history

def get_tools() -> list:
    return [
        {
            "name": "get_user_balance",
            "description": "Obtenir la repartition de la valeur du patrimoine de l'utilisateur (actions, crypto, cash, valeur totale)."
            "Il doit etre utilise avant tout autre outil pour que l'agent puisse se faire une idee de la situation globale de l'utilisateur. "
            "Possibilite de demander les details des comptes et positions pour une vue plus granulaire.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "details": {
                        "type": "boolean",
                        "description": "Boolean pour inclure le detail des comptes et positions. (default: false)",
                        "default": False
                    },
                    "date": {
                        "type": "string",
                        "description": "Optionnel. La date cible sous format 'YYYY-MM-DD' uniquement si l'utilisateur demande explicitement un etat de son patrimoine a une date passee.",
                    }
                }
            }
        },
        {
            "name": "get_historical_performance",
            "description": "Obtenir la performance dynamique et les plus-values (PnL) des investissements de l'utilisateur sur une periode donnee pour observer son evolution temporelle."
            "L'agent peut utiliser cet outil pour analyser la tendance de l'utilisateur et observer les differences sur un laps de temps."
            "Ce tool est a utiliser EXCLUSIVEMENT pour une analyse de l'evolution et de sa performance historique, pas pour la photo a un instant T du patrimoine (utiliser get_user_balance).",
            "input_schema": {
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "Le nombre de jours de l'historique de perfromance a recuperer (ex: 7 pour la derniere semaine, 30 pour le dernier mois).",
                        "default": 10
                    },
                    "account_type": {
                        "type": "string",
                        "description": "Le type de compte pour lequel recuperer la performance (ex: 'stock' ou 'crypto' ou 'all').",
                        "default": "stock"
                    }
                }
            }
        },
        {
            "name": "get_user_cashflow",
            "description": "Obtenir les statistiques liees au budget (revenus/inflows, depenses/outflows, epargne/savings) de l'utilisateur.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "details": {
                        "type": "boolean",
                        "description": "Boolean pour inclure le detail brut des cashflows.",
                        "default": False
                    },
                    "flow_type": {
                        "type": "string",
                        "description": "Permet de filtrer specifiquement pour un type de transaction (ex: 'inflow' ou 'outflow'). Laisser vide pour avoir la vue globale.",
                        "enum": ["inflow", "outflow"]
                    }
                }
            }
        },
        {
            "name": "get_performance_since_last_login",
            "description": "Recupere la performance du patrimoine depuis la derniere connexion de l'utilisateur, en calculant la variation sur ses comptes actions et cryptos.",
            "input_schema": {
                "type": "object",
                "properties": {}
            }
        }
    ]

def get_user_balance(session: Session, user_uuid: str, master_key: bytes, details: bool = False, date: str = None) -> dict:
    user_bidx = hash_index(user_uuid, master_key)
    settings = get_or_create_settings(session, user_uuid, master_key)

    result = {}

    # --- Stock Accounts ---
    stock_models = session.exec(
        select(StockAccount).where(StockAccount.user_uuid_bidx == user_bidx)
    ).all()

    stock_current_value = Decimal(0)
    stock_accounts_details = []
    for acc in stock_models:
        transactions = get_stock_transactions(session, acc.uuid, master_key)
        summary = get_stock_account_summary(session, transactions, as_of=date, db_only=True)
        acc_val = summary.current_value or Decimal(0)
        stock_current_value += acc_val
        
        if details:
            acc_name = decrypt_data(acc.name_enc, master_key)
            positions = [{"symbol": p.symbol, "amount": float(p.total_amount), "current_value": float(p.current_value) if p.current_value else 0.0} for p in summary.positions if p.total_amount != 0]
            stock_accounts_details.append({
                "name": acc_name,
                "total_value": float(acc_val),
                "positions": positions
            })

    # --- Crypto Accounts ---
    crypto_models = session.exec(
        select(CryptoAccount).where(CryptoAccount.user_uuid_bidx == user_bidx)
    ).all()

    crypto_current_value = Decimal(0)
    crypto_accounts_details = []
    for acc in crypto_models:
        transactions = get_crypto_transactions(session, acc.uuid, master_key)
        summary = get_crypto_account_summary(session, transactions, as_of=date, db_only=True)
        acc_val = summary.current_value or Decimal(0)
        crypto_current_value += acc_val
        
        if details:
            acc_name = decrypt_data(acc.name_enc, master_key)
            positions = [{"symbol": p.symbol, "amount": float(p.total_amount), "current_value": float(p.current_value) if p.current_value else 0.0} for p in summary.positions if p.total_amount != 0]
            crypto_accounts_details.append({
                "name": acc_name,
                "total_value": float(acc_val),
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

    result.update({
        "stocks_total": float(stock_current_value),
        "crypto_total": float(crypto_current_value),
        "cash_total": float(cash_total),
        "assets_total": float(assets_total),
        "global_wealth": float(stock_current_value + crypto_current_value + cash_total + assets_total)
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


def get_tool_registry() -> dict[str, Callable[..., Any]]:
    return {
        "get_user_balance": get_user_balance,
        "get_historical_performance": get_historical_performance,
        "get_user_cashflow": get_user_cashflow,
        "get_performance_since_last_login": get_performance_since_last_login,
    }


def get_user_statistics(session: Session, user_uuid: str, master_key: bytes):
    # TODO : implement monthly deposits, withdrawals, number of transactions, number of positions, history pnl, by account type etc...
    pass
