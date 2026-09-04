"""Cashflow schemas."""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel

from models.currency import BASE_CURRENCY
from models.enums import FlowType, Frequency


class CashflowCreate(BaseModel):
    """Create a cashflow."""
    name: str
    flow_type: FlowType
    category: str
    amount: Decimal
    frequency: Frequency
    transaction_date: date
    bank_account_id: str | None = None
    is_active: bool = True


class CashflowUpdate(BaseModel):
    """Update a cashflow."""
    name: str | None = None
    flow_type: FlowType | None = None
    category: str | None = None
    amount: Decimal | None = None
    frequency: Frequency | None = None
    transaction_date: date | None = None
    bank_account_id: str | None = None
    is_active: bool | None = None


class CashflowResponse(BaseModel):
    """Single cashflow response."""
    id: str
    name: str
    flow_type: FlowType
    category: str
    amount: Decimal
    frequency: Frequency
    transaction_date: date
    created_at: datetime
    updated_at: datetime

    monthly_amount: Decimal  # Amount normalized to monthly, in `currency`
    # The same figure in euros, for callers that aggregate themselves and hold
    # no exchange rates. None when this flow's currency has no published rate.
    monthly_amount_eur: Decimal | None = None
    bank_account_id: str | None = None  # Linked bank account UUID
    # Read-only: the currency of the linked account, euros when there is none.
    # Never submitted — a flow is denominated by the account it hits, not by a
    # choice of its own. See docs/currencies.md.
    currency: str = BASE_CURRENCY
    is_active: bool = True  # False = excluded from the automatic bank balance sync


# Every total below is in euros, and `None` when one of the currencies in play
# has no published rate. A figure is dropped rather than made up: converting at
# a rate of 1 would add francs to euros with nothing marking the total as wrong.
# Same rule, and same reason, as `BankSummaryResponse.total_balance`.
class CashflowCategoryResponse(BaseModel):
    """Cashflows grouped by category."""
    category: str
    total_amount: Decimal | None
    monthly_total: Decimal | None
    count: int
    items: list[CashflowResponse]


class CashflowSummaryResponse(BaseModel):
    """Summary of cashflows (inflows or outflows)."""
    flow_type: FlowType
    total_amount: Decimal | None
    monthly_total: Decimal | None
    categories: list[CashflowCategoryResponse]


class CashflowBalanceResponse(BaseModel):
    """Balance between inflows and outflows."""
    total_inflows: Decimal | None
    monthly_inflows: Decimal | None
    total_outflows: Decimal | None
    monthly_outflows: Decimal | None
    net_balance: Decimal | None
    monthly_balance: Decimal | None
    savings_rate: Decimal | None = None  # (inflows - outflows) / inflows * 100
    inflows: CashflowSummaryResponse
    outflows: CashflowSummaryResponse


# ---------------------------------------------------------------------------
# Declared vs observed
# ---------------------------------------------------------------------------


class RecentOccurrence(BaseModel):
    """One real movement behind a declaration, for the "3 derniers" line."""
    day: date
    amount: Decimal


class MatchCandidate(BaseModel):
    """One group of real movements that could be a declaration's counterpart."""
    pattern: str
    observed_amount: Decimal
    occurrences: int
    last_seen: date


class CashflowMatchUpdate(BaseModel):
    """Confirm or clear the label a declaration is matched against.

    `null` or an empty string unlinks it and puts the app back to suggesting.
    """
    match_pattern: str | None = None


class CashflowComparison(BaseModel):
    """What one declaration says, against what actually moved.

    `status` is the verdict: `unmatched` (never confirmed, a suggestion may be
    attached), `missing` (confirmed but nothing has moved for a couple of
    cadences), `duplicated` (seen twice where once was declared), `drifted` (it
    moves, for another amount) or `on_track`.
    """
    cashflow_id: str
    name: str
    flow_type: FlowType
    frequency: Frequency
    category: str
    declared_amount: Decimal
    status: str
    match_pattern: str | None = None
    observed_amount: Decimal | None = None
    last_seen: date | None = None
    occurrences: int = 0
    recent: list[RecentOccurrence] = []
    # Only on `unmatched`: what this declaration could be, best first. Several,
    # because amount and spacing cannot always tell two recurrences apart.
    candidates: list[MatchCandidate] = []
