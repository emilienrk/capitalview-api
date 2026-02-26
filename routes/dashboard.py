"""Dashboard routes - Personal portfolio overview."""

from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlmodel import Session, select

from database import get_session
from models import User, StockAccount, CryptoAccount
from dtos import PortfolioResponse
from dtos.dashboard import DashboardStatisticsResponse, InvestmentDistribution, WealthBreakdown
from services.auth import get_current_user, get_master_key
from services.encryption import hash_index
from services.exchange_rate import get_exchange_rate, get_effective_usd_eur_rate, convert_crypto_prices_to_eur
from services.settings import get_or_create_settings
from services.stock_transaction import get_stock_account_summary
from services.crypto_transaction import get_crypto_account_summary
from services.bank import get_user_bank_accounts
from services.asset import get_user_assets

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])


@router.get("/exchange-rate")
def get_rate(
    current_user: Annotated[User, Depends(get_current_user)],
    from_currency: str = "USD",
    to_currency: str = "EUR",
) -> dict:
    """Return a cached exchange rate (e.g. USD→EUR) for frontend use."""
    rate = get_exchange_rate(from_currency, to_currency)
    return {
        "from": from_currency,
        "to": to_currency,
        "rate": float(rate),
    }


@router.get("/portfolio", response_model=PortfolioResponse)
def get_my_portfolio(
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
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
        summary = get_stock_account_summary(session, acc, master_key)
        accounts.append(summary)
    
    # Convert crypto accounts from USD to EUR for portfolio aggregation
    settings = get_or_create_settings(session, current_user.uuid, master_key)
    usd_eur_rate = get_effective_usd_eur_rate(
        float(settings.usd_eur_rate) if settings.usd_eur_rate is not None else None
    )
    for acc in crypto_models:
        summary = get_crypto_account_summary(session, acc, master_key)
        summary_eur = convert_crypto_prices_to_eur(summary, usd_eur_rate)
        accounts.append(summary_eur)
    
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
        summary = get_stock_account_summary(session, acc, master_key)
        stock_invested += summary.total_invested
        if summary.current_value:
            stock_current_value += summary.current_value

    # ── Crypto accounts (converted to EUR) ──────────────────
    crypto_models = session.exec(
        select(CryptoAccount).where(CryptoAccount.user_uuid_bidx == user_bidx)
    ).all()

    settings = get_or_create_settings(session, current_user.uuid, master_key)
    usd_eur_rate = get_effective_usd_eur_rate(
        float(settings.usd_eur_rate) if settings.usd_eur_rate is not None else None
    )
    crypto_invested = Decimal(0)
    crypto_current_value = Decimal(0)
    for acc in crypto_models:
        summary = get_crypto_account_summary(session, acc, master_key)
        summary_eur = convert_crypto_prices_to_eur(summary, usd_eur_rate)
        crypto_invested += summary_eur.total_invested
        if summary_eur.current_value:
            crypto_current_value += summary_eur.current_value

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
    bank_summary = get_user_bank_accounts(session, current_user.uuid, master_key)
    cash_total = bank_summary.total_balance

    # ── Assets (personal possessions, unsold) ───────────────
    asset_summary = get_user_assets(session, current_user.uuid, master_key)
    assets_total = asset_summary.total_estimated_value

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