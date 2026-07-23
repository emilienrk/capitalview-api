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
    CardResponse,
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
from services.projection import generate_wealth_projection
from dtos.projection import ProjectionParameters

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
    
    # ── Projection ──
    # Create default parameters (120 months)
    params = ProjectionParameters(months_to_project=120)
    projection = generate_wealth_projection(session, current_user, master_key, params)

    return DashboardSummaryResponse(
        statistics=statistics,
        portfolio=portfolio,
        cashflow=cashflow,
        projection=projection,
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
    stock_snaps = {s.snapshot_date: s.total_value for s in get_all_stock_accounts_history(session, current_user.uuid, master_key, include_current=False)}
    crypto_snaps = {s.snapshot_date: s.total_value for s in get_all_crypto_accounts_history(session, current_user.uuid, master_key, include_current=False)}

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
    
    for acc in crypto_models:
        transactions = get_crypto_transactions(session, acc.uuid, master_key)

        summary = get_crypto_account_summary(session, transactions, db_only=db_only)
        accounts.append(
            PortfolioAccountSummaryResponse(
                account_id=acc.uuid,
                account_name=decrypt_data(acc.name_enc, master_key),
                account_type="CRYPTO",
                **summary.model_dump(),
            )
        )
    
    total_invested = sum(a.total_invested for a in accounts)
    total_deposits = sum(a.total_deposits for a in accounts)
    total_withdrawals = sum(a.total_withdrawals for a in accounts)
    total_fees = sum(a.total_fees for a in accounts)
    # VALEUR is holdings-scoped (idle cash sits in each account's cash_balance).
    current_value = sum(a.current_value for a in accounts if a.current_value)

    profit_loss = None
    profit_loss_pct = None

    # Portfolio P/L on cost basis (PRU): VALEUR - INVESTI, matching each account.
    account_pnl = [a.profit_loss for a in accounts if a.profit_loss is not None]
    if account_pnl:
        profit_loss = sum(account_pnl)
        if total_invested > 0:
            profit_loss_pct = (profit_loss / total_invested * 100)

    return PortfolioResponse(
        total_invested=round(total_invested, 2),
        total_deposits=round(total_deposits, 2),
        total_withdrawals=round(total_withdrawals, 2),
        total_fees=round(total_fees, 2),
        current_value=round(current_value, 2) if current_value else None,
        profit_loss=round(profit_loss, 2) if profit_loss is not None else None,
        profit_loss_percentage=round(profit_loss_pct, 2) if profit_loss_pct is not None else None,
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

    liquidity = Decimal(0)

    # ── Stock accounts ──────────────────────────────────────
    stock_models = session.exec(
        select(StockAccount).where(StockAccount.user_uuid_bidx == user_bidx)
    ).all()

    stock_invested = Decimal(0)
    stock_current_value = Decimal(0)
    stock_deposits = Decimal(0)
    stock_withdrawals = Decimal(0)
    for acc in stock_models:
        transactions = get_stock_transactions(session, acc.uuid, master_key)
        summary = get_stock_account_summary(session, transactions, db_only=db_only)

        stock_invested += summary.total_invested
        stock_deposits += summary.total_deposits
        stock_withdrawals += summary.total_withdrawals
        liquidity += summary.cash_balance

        if summary.current_value:
            stock_current_value += summary.current_value

    # ── Crypto accounts ──────────────────────────────────────
    crypto_models = session.exec(
        select(CryptoAccount).where(CryptoAccount.user_uuid_bidx == user_bidx)
    ).all()

    settings = get_or_create_settings(session, current_user.uuid, master_key)
    crypto_invested = Decimal(0)
    crypto_current_value = Decimal(0)
    crypto_deposits = Decimal(0)
    crypto_withdrawals = Decimal(0)
    for acc in crypto_models:
        transactions = get_crypto_transactions(session, acc.uuid, master_key)

        summary = get_crypto_account_summary(session, transactions, db_only=db_only)
        crypto_invested += summary.total_invested
        crypto_deposits += summary.total_deposits
        crypto_withdrawals += summary.total_withdrawals
        liquidity += summary.cash_balance

        if summary.current_value:
            crypto_current_value += summary.current_value

    investment_net_deposits = (stock_deposits + crypto_deposits) - (stock_withdrawals + crypto_withdrawals)

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
        total_deposits=round(investment_net_deposits, 2),
        total_withdrawals=round(stock_withdrawals + crypto_withdrawals, 2),
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
    investments_total = total_investment_value + liquidity
    total_wealth = cash_total + investments_total + assets_total
    total_deposits = investment_net_deposits + cash_total + assets_total

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
        total_deposits=round(total_deposits, 2),
        total_withdrawals=round(stock_withdrawals + crypto_withdrawals, 2),
        total_wealth=round(total_wealth, 2),
    )

    return DashboardStatisticsResponse(
        distribution=distribution,
        wealth=wealth,
    )
@router.get("/card", response_model=CardResponse)
async def get_or_generate_card(
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """
    Returns the card for today. If it doesn't exist, generates one.
    """
    from datetime import datetime, timedelta, timezone
    import random
    from models.card import Card
    from models.enums import CardTheme
    from services.ai.agents.card_agent import CardAgent
    from services.ai.tools import get_performance_since_last_login
    from services.encryption import encrypt_data, decrypt_data, hash_index

    user_bidx = hash_index(current_user.uuid, master_key)
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # Check if a card exists for today
    today_card = session.exec(
        select(Card)
        .where(Card.user_uuid_bidx == user_bidx)
        .where(Card.created_at >= today_start)
    ).first()

    if today_card:
        try:
            return CardResponse(
                uuid=today_card.uuid,
                title=decrypt_data(today_card.title_enc, master_key),
                theme=decrypt_data(today_card.theme_enc, master_key),
                text=decrypt_data(today_card.text_enc, master_key),
                scope=decrypt_data(today_card.scope_enc, master_key),
                created_at=today_card.created_at
            )
        except Exception:
            pass

    # Check recent cards to avoid repeating monthly/weekly
    thirty_days_ago = now - timedelta(days=35)
    recent_cards = session.exec(
        select(Card)
        .where(Card.user_uuid_bidx == user_bidx)
        .where(Card.created_at >= thirty_days_ago)
        .order_by(Card.created_at.desc())
    ).all()

    recent_themes = set()
    last_monthly = None
    last_weekly = None
    
    for card in recent_cards:
        try:
            th = decrypt_data(card.theme_enc, master_key)
            recent_themes.add(th)
            if th == CardTheme.MONTHLY.value and last_monthly is None:
                last_monthly = card.created_at
            if th == CardTheme.WEEKLY.value and last_weekly is None:
                last_weekly = card.created_at
        except Exception:
            continue

    # Determine Theme
    selected_theme = None
    theme_context = ""
    is_significant = False
    perf_data = {}

    import calendar
    _, days_in_month = calendar.monthrange(now.year, now.month)

    # 1. Monthly condition
    # If the month is over (or very near the end) and we haven't done it 
    # Let's say if we are in the first 5 days of the month and no MONTHLY for the previous month
    # Or if we are in the last 2 days of the month and no MONTHLY this month
    is_start_of_month = now.day <= 5
    is_end_of_month = now.day >= days_in_month - 1
    
    if is_end_of_month and (last_monthly is None or (now - last_monthly).days > 25):
        selected_theme = CardTheme.MONTHLY.value
        theme_context = "Thème : Bilan mensuel. Rédige un bref bilan du mois qui s'écoule pour le portefeuille de l'utilisateur."
    elif is_start_of_month and (last_monthly is None or (now - last_monthly).days > 25):
        selected_theme = CardTheme.MONTHLY.value
        theme_context = "Thème : Bilan mensuel. Rédige un résumé du mois précédent pour les finances de l'utilisateur."

    # 2. Weekly condition
    if not selected_theme:
        is_weekend = now.weekday() in [5, 6] # Saturday, Sunday
        if is_weekend and (last_weekly is None or (now - last_weekly).days > 5):
            selected_theme = CardTheme.WEEKLY.value
            theme_context = "Thème : Bilan de la semaine. Rédige un aperçu des événements de la semaine écoulée."

    # 3. Performance / Significant
    if not selected_theme:
        perf_data = get_performance_since_last_login(session, current_user.uuid, master_key)
        is_significant = perf_data.get("is_significant", False)
        if is_significant:
            selected_theme = (
                CardTheme.TREND.value
                if perf_data.get("total_absolute_change_eur", 0) >= 0
                else CardTheme.ISSUE.value
            )
            theme_context = (
                f"Thème prioritaire basé sur la variation depuis la dernière connexion : {selected_theme}\n"
                "RÉDIGE OBLIGATOIREMENT une card expliquant l'évolution du patrimoine sur les derniers jours. Liste simplement les faits."
            )

    # 4. Random fallback
    if not selected_theme:
        # Exclude monthly and weekly from random themes
        choices = [t.value for t in CardTheme if t.value not in [CardTheme.MONTHLY.value, CardTheme.WEEKLY.value]]
        selected_theme = random.choice(choices)
        theme_context = f"Thème tiré au sort pour cette carte : {selected_theme}\n"

    # Call agent
    agent = CardAgent(current_user.uuid, session, master_key.encode() if isinstance(master_key, str) else master_key)
    card_data = await agent.main(
        theme_context=theme_context,
        selected_theme=selected_theme,
        recent_cards=recent_cards,
        is_significant=is_significant,
        perf_data=perf_data
    )
    
    card_title = card_data.get("title", selected_theme)
    card_body = card_data.get("body", str(card_data))

    new_card = Card(
        user_uuid_bidx=user_bidx,
        title_enc=encrypt_data(card_title, master_key),
        text_enc=encrypt_data(card_body, master_key),
        theme_enc=encrypt_data(selected_theme, master_key),
        scope_enc=encrypt_data("GLOBAL", master_key)
    )
    session.add(new_card)
    session.commit()
    session.refresh(new_card)

    return CardResponse(
        uuid=new_card.uuid,
        title=card_title,
        theme=selected_theme,
        text=card_body,
        scope="GLOBAL",
        created_at=new_card.created_at
    )
