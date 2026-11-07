"""Investor behaviour analytics routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlmodel import Session

from database import get_session
from dtos import AnalysedAssetOut, InvestorAnalyticsResponse, YearlyPerformanceResponse
from models import User
from services.analytics.cache import KIND_INVESTOR, KIND_YEARLY, cached_payload
from services.analytics.report import build_investor_analytics
from services.analytics.universe import build_asset_universe
from services.analytics.yearly import yearly_payload, yearly_performance
from services.auth import get_current_user, get_master_key
from services.stock_account import get_user_stock_accounts
from services.stock_transaction import get_account_transactions

router = APIRouter(prefix="/analytics", tags=["Analytics"])


_REFRESH = Query(
    False,
    description=(
        "Rebuild the analysis now instead of serving the stored one. A ledger or "
        "settings change rebuilds it anyway; this is for the price drift in between."
    ),
)


@router.get("/investor", response_model=InvestorAnalyticsResponse)
def get_investor_analytics(
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
    refresh: bool = _REFRESH,
):
    """Behavioural analysis of the user's stock investing over the full history.

    Served from the store while the ledger and the settings it was built from
    are unchanged and its validity holds (`computed_at`, `valid_until`).
    """
    return cached_payload(
        session,
        current_user.uuid,
        master_key,
        KIND_INVESTOR,
        InvestorAnalyticsResponse,
        lambda: build_investor_analytics(session, current_user.uuid, master_key),
        refresh=refresh,
    )


@router.get("/yearly", response_model=YearlyPerformanceResponse)
def get_yearly_performance(
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
    refresh: bool = _REFRESH,
):
    """Each calendar year: gain net of deposits and return per pocket, beside the benchmark."""
    return cached_payload(
        session,
        current_user.uuid,
        master_key,
        KIND_YEARLY,
        YearlyPerformanceResponse,
        lambda: yearly_payload(yearly_performance(session, current_user.uuid, master_key)),
        refresh=refresh,
    )


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
