"""Investor behaviour analytics routes."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlmodel import Session

from database import get_session
from dtos import AnalysedAssetOut, InvestorAnalyticsResponse
from models import User
from services.analytics.report import build_investor_analytics
from services.analytics.universe import build_asset_universe
from services.auth import get_current_user, get_master_key
from services.stock_account import get_user_stock_accounts
from services.stock_transaction import get_account_transactions

router = APIRouter(prefix="/analytics", tags=["Analytics"])


@router.get("/investor", response_model=InvestorAnalyticsResponse)
def get_investor_analytics(
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """Behavioural analysis of the user's stock investing over the full history."""
    return build_investor_analytics(session, current_user.uuid, master_key)


@router.get("/assets", response_model=list[AnalysedAssetOut])
def list_analysed_assets(
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """Lines the user has traded, to pick from instead of typing an ISIN.

    Separate from /investor on purpose: the settings forms need this list even
    when the analysis itself has too little history to compute, and it must not
    wait behind a full replay to populate a dropdown.
    """
    accounts = get_user_stock_accounts(session, current_user.uuid, master_key)
    transactions = []
    for account in accounts:
        transactions.extend(get_account_transactions(session, account.id, master_key))
    return build_asset_universe(session, transactions)
