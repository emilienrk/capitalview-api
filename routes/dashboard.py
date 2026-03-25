"""Dashboard routes - Personal portfolio overview."""

from collections import defaultdict
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlmodel import Session, select

from database import get_session
from models import User, StockAccount, CryptoAccount
from dtos import PortfolioResponse, PortfolioAccountSummaryResponse
from dtos.dashboard import (
    DashboardStatisticsResponse,
    DashboardSummaryResponse,
    GlobalHistorySnapshotResponse,
    InvestmentDistribution,
    WealthBreakdown,
)
from services.auth import get_current_user, get_master_key
from services.encryption import hash_index, decrypt_data
from services.market import get_exchange_rate
from services.settings import get_or_create_settings
from services.stock_transaction import get_stock_account_summary, get_account_transactions as get_stock_transactions
from services.crypto_transaction import get_crypto_account_summary, get_account_transactions as get_crypto_transactions
from services.bank import get_user_bank_accounts, get_all_bank_accounts_history
from services.asset import get_user_assets, get_asset_portfolio_history
from services.stock_account import get_all_stock_accounts_history
from services.crypto_account import get_all_crypto_accounts_history
from services.cashflow import get_user_cashflow_balance

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])


@router.get("/summary", response_model=DashboardSummaryResponse)
def get_dashboard_summary(
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """
    Single endpoint returning the complete financial picture for AI agent use:
    - Dashboard statistics (wealth breakdown, stock/crypto distribution)
    - Portfolio (all accounts with PnL)
    - Cashflow balance (inflows, outflows, savings rate)
    """
    statistics = get_dashboard_statistics(current_user, master_key, session)
    portfolio = get_my_portfolio(current_user, master_key, session)
    cashflow = get_user_cashflow_balance(session, current_user.uuid, master_key)

    return DashboardSummaryResponse(
        statistics=statistics,
        portfolio=portfolio,
        cashflow=cashflow,
    )


@router.get("/exchange-rate")
def get_rate(
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[Session, Depends(get_session)],
    from_currency: str = "USD",
    to_currency: str = "EUR",
) -> dict:
    """Return a cached exchange rate (e.g. USD→EUR) for frontend use."""
    rate = get_exchange_rate(session, from_currency, to_currency)
    return {
        "from": from_currency,
        "to": to_currency,
        "rate": float(rate),
    }


@router.get("/history", response_model=list[GlobalHistorySnapshotResponse])
def get_global_history(
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """
    Aggregated daily wealth history across all account types (stock, crypto, bank, assets).
    Returns one entry per day with total_wealth and a breakdown by category.
    No positions included — designed for lightweight chart rendering.
    """
    settings = get_or_create_settings(session, current_user.uuid, master_key)

    # Fetch per-category histories in parallel (same session, sequential is fine)
    stock_snaps = {s.snapshot_date: s.total_value for s in get_all_stock_accounts_history(session, current_user.uuid, master_key)}
    crypto_snaps = {s.snapshot_date: s.total_value for s in get_all_crypto_accounts_history(session, current_user.uuid, master_key)}

    bank_snaps: dict = {}
    if settings.bank_module_enabled:
        bank_snaps = {s.snapshot_date: s.total_value for s in get_all_bank_accounts_history(session, current_user.uuid, master_key)}

    assets_snaps: dict = {}
    if settings.wealth_module_enabled:
        assets_snaps = {s.snapshot_date: s.total_value for s in get_asset_portfolio_history(session, current_user.uuid, master_key)}

    # Union of all dates
    all_dates = sorted(
        stock_snaps.keys() | crypto_snaps.keys() | bank_snaps.keys() | assets_snaps.keys()
    )

    result = []
    for d in all_dates:
        stock_v = stock_snaps.get(d, Decimal("0"))
        crypto_v = crypto_snaps.get(d, Decimal("0"))
        bank_v = bank_snaps.get(d, Decimal("0"))
        assets_v = assets_snaps.get(d, Decimal("0"))
        result.append(
            GlobalHistorySnapshotResponse(
                snapshot_date=d,
                total_wealth=stock_v + crypto_v + bank_v + assets_v,
                stock_value=stock_v,
                crypto_value=crypto_v,
                bank_value=bank_v,
                assets_value=assets_v,
            )
        )

    return result


@router.get("/portfolio", response_model=PortfolioResponse)
def get_my_portfolio(
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
    db_only: bool = False,
):
    """
    Get complete portfolio for authenticated user.
    
    Aggregates all stock and crypto accounts with:
    - Total invested amount
    - Total fees
    - Current value
    - Profit/Loss
    - Performance percentage
    """
    user_bidx = hash_index(current_user.uuid, master_key)
    
    stock_models = session.exec(
        select(StockAccount).where(StockAccount.user_uuid_bidx == user_bidx)
    ).all()
    
    crypto_models = session.exec(
        select(CryptoAccount).where(CryptoAccount.user_uuid_bidx == user_bidx)
    ).all()
    
    accounts = []
    
    for acc in stock_models:
        transactions = get_stock_transactions(session, acc.uuid, master_key)

        summary = get_stock_account_summary(session, transactions, db_only=db_only)
        accounts.append(
            PortfolioAccountSummaryResponse(
                account_id=acc.uuid,
                account_name=decrypt_data(acc.name_enc, master_key),
                account_type=decrypt_data(acc.account_type_enc, master_key),
                **summary.model_dump(),
            )
        )
    
    # Convert crypto accounts — prices are now stored in EUR in market_price_history
    settings = get_or_create_settings(session, current_user.uuid, master_key)
    for acc in crypto_models:
        transactions = get_crypto_transactions(session, acc.uuid, master_key)

        summary = get_crypto_account_summary(session, transactions, settings.crypto_show_negative_positions, db_only=db_only)
        accounts.append(
            PortfolioAccountSummaryResponse(
                account_id=acc.uuid,
                account_name=decrypt_data(acc.name_enc, master_key),
                account_type="CRYPTO",
                **summary.model_dump(),
            )
        )
    
    total_invested = sum(a.total_invested for a in accounts)
    total_fees = sum(a.total_fees for a in accounts)
    current_value = sum(a.current_value for a in accounts if a.current_value)
    
    profit_loss = None
    profit_loss_pct = None
    
    if current_value > 0 or total_invested > 0:
        profit_loss = current_value - total_invested
        if total_invested > 0:
            profit_loss_pct = (profit_loss / total_invested * 100)
    
    return PortfolioResponse(
        total_invested=round(total_invested, 2),
        total_fees=round(total_fees, 2),
        current_value=round(current_value, 2) if current_value else None,
        profit_loss=round(profit_loss, 2) if profit_loss else None,
        profit_loss_percentage=round(profit_loss_pct, 2) if profit_loss_pct else None,
        accounts=accounts
    )


@router.get("/statistics", response_model=DashboardStatisticsResponse)
def get_dashboard_statistics(
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
    db_only: bool = False,
):
    """
    Compute aggregated dashboard statistics:
    - Stock vs Crypto distribution (invested & current value)
    - Cash / Investments / Assets wealth breakdown
    """
    user_bidx = hash_index(current_user.uuid, master_key)

    # ── Stock accounts ──────────────────────────────────────
    stock_models = session.exec(
        select(StockAccount).where(StockAccount.user_uuid_bidx == user_bidx)
    ).all()

    stock_invested = Decimal(0)
    stock_current_value = Decimal(0)
    for acc in stock_models:
        transactions = get_stock_transactions(session, acc.uuid, master_key)
        summary = get_stock_account_summary(session, transactions, db_only=db_only)

        stock_invested += summary.total_invested
        if summary.current_value:
            stock_current_value += summary.current_value

    # ── Crypto accounts ──────────────────────────────────────
    crypto_models = session.exec(
        select(CryptoAccount).where(CryptoAccount.user_uuid_bidx == user_bidx)
    ).all()

    settings = get_or_create_settings(session, current_user.uuid, master_key)
    crypto_invested = Decimal(0)
    crypto_current_value = Decimal(0)
    for acc in crypto_models:
        transactions = get_crypto_transactions(session, acc.uuid, master_key)

        summary = get_crypto_account_summary(session, transactions, settings.crypto_show_negative_positions, db_only=db_only)
        crypto_invested += summary.total_invested
        if summary.current_value:
            crypto_current_value += summary.current_value

    # ── Investment distribution percentages ─────────────────
    total_investment_value = stock_current_value + crypto_current_value
    stock_pct = None
    crypto_pct = None
    if total_investment_value > 0:
        stock_pct = round(stock_current_value / total_investment_value * 100, 2)
        crypto_pct = round(crypto_current_value / total_investment_value * 100, 2)

    distribution = InvestmentDistribution(
        stock_invested=round(stock_invested, 2),
        stock_current_value=round(stock_current_value, 2),
        stock_percentage=stock_pct,
        crypto_invested=round(crypto_invested, 2),
        crypto_current_value=round(crypto_current_value, 2),
        crypto_percentage=crypto_pct,
    )

    # ── Cash (bank balances) ────────────────────────────────
    if settings.bank_module_enabled:
        bank_summary = get_user_bank_accounts(session, current_user.uuid, master_key)
        cash_total = bank_summary.total_balance
    else:
        cash_total = Decimal(0)

    # ── Assets (personal possessions, unsold) ───────────────
    if settings.wealth_module_enabled:
        asset_summary = get_user_assets(session, current_user.uuid, master_key)
        assets_total = asset_summary.total_estimated_value
    else:
        assets_total = Decimal(0)

    # ── Total wealth ────────────────────────────────────────
    investments_total = total_investment_value
    total_wealth = cash_total + investments_total + assets_total

    cash_pct = None
    inv_pct = None
    assets_pct = None
    if total_wealth > 0:
        cash_pct = round(cash_total / total_wealth * 100, 2)
        inv_pct = round(investments_total / total_wealth * 100, 2)
        assets_pct = round(assets_total / total_wealth * 100, 2)

    wealth = WealthBreakdown(
        cash=round(cash_total, 2),
        cash_percentage=cash_pct,
        investments=round(investments_total, 2),
        investments_percentage=inv_pct,
        assets=round(assets_total, 2),
        assets_percentage=assets_pct,
        total_wealth=round(total_wealth, 2),
    )

    return DashboardStatisticsResponse(
        distribution=distribution,
        wealth=wealth,
    )