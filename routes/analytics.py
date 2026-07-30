"""Investor behaviour analytics routes."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlmodel import Session

from database import get_session
from dtos import InvestorAnalyticsResponse
from models import User
from services.analytics.report import build_investor_analytics
from services.auth import get_current_user, get_master_key

router = APIRouter(prefix="/analytics", tags=["Analytics"])


@router.get("/investor", response_model=InvestorAnalyticsResponse)
def get_investor_analytics(
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """Behavioural analysis of the user's stock investing over the full history."""
    return build_investor_analytics(session, current_user.uuid, master_key)
