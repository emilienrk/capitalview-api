"""Placement schemas: AV, PER, SCPI and the like, followed by hand."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Annotated

from pydantic import AfterValidator, BaseModel, Field, model_validator

from models.enums import PlacementType, PlacementEntryType


def _not_in_future(v: date | None) -> date | None:
    # Placements routinely predate 2000, so the shared ValidDate* floor does not fit.
    if v is None:
        return v
    if v.year < 1950:
        raise ValueError("La date ne peut pas être avant 1950.")
    if v > datetime.now(timezone.utc).date() + timedelta(days=1):
        raise ValueError("La date ne peut pas être dans le futur.")
    return v


PlacementDateOpt = Annotated[date | None, AfterValidator(_not_in_future)]
PlacementDate = Annotated[date, AfterValidator(_not_in_future)]


class PlacementAccountCreate(BaseModel):
    """Create a placement."""
    name: str = Field(min_length=1)
    placement_type: PlacementType = PlacementType.AV
    institution_name: str | None = None
    opened_at: PlacementDateOpt = None
    expected_return_rate: Decimal | None = Field(default=None, ge=-1, le=1)


class PlacementAccountUpdate(BaseModel):
    """Update a placement. A field left out is kept; null clears the optional ones."""
    name: str | None = Field(default=None, min_length=1)
    placement_type: PlacementType | None = None
    institution_name: str | None = None
    opened_at: PlacementDateOpt = None
    expected_return_rate: Decimal | None = Field(default=None, ge=-1, le=1)


class PlacementEntryCreate(BaseModel):
    """A statement balance, a deposit or a withdrawal."""
    type: PlacementEntryType
    amount: Decimal = Field(ge=0)
    occurred_at: PlacementDate
    note: str | None = None

    @model_validator(mode="after")
    def flows_are_positive(self) -> "PlacementEntryCreate":
        if self.type != PlacementEntryType.VALUATION and self.amount <= 0:
            raise ValueError("Le montant d'un versement ou d'un rachat doit être positif.")
        return self


class PlacementEntryUpdate(BaseModel):
    """Update an entry. Its type stays: a balance and a deposit mean different things."""
    amount: Decimal | None = Field(default=None, ge=0)
    occurred_at: PlacementDateOpt = None
    note: str | None = None

    @model_validator(mode="after")
    def at_least_one_field(self) -> "PlacementEntryUpdate":
        if not self.model_fields_set:
            raise ValueError("Au moins un champ doit être fourni")
        return self


class PlacementEntryResponse(BaseModel):
    id: str
    account_id: str
    type: PlacementEntryType
    amount: Decimal
    occurred_at: date
    note: str | None = None
    created_at: datetime
    updated_at: datetime


class PlacementAccountResponse(BaseModel):
    """A placement and what its entries say about it today."""
    id: str
    name: str
    placement_type: PlacementType
    institution_name: str | None = None
    opened_at: date | None = None
    expected_return_rate: Decimal | None = None

    current_value: Decimal
    total_deposits: Decimal
    total_withdrawals: Decimal
    net_invested: Decimal
    # None until a balance has been entered: without one, the value is only
    # what was paid in, and a gain of zero would be a claim, not a reading.
    gain: Decimal | None = None
    gain_percentage: Decimal | None = None
    last_valuation_date: date | None = None
    last_valuation_value: Decimal | None = None
    days_since_valuation: int | None = None
    is_stale: bool = False
    # Time-weighted, between statements. None under a year of statements.
    annual_return_rate: Decimal | None = None
    return_days: int = 0
    # AV only: the day withdrawals start benefiting from the yearly allowance.
    tax_anniversary_date: date | None = None

    created_at: datetime
    updated_at: datetime


class PlacementSummaryResponse(BaseModel):
    total_value: Decimal
    total_deposits: Decimal
    total_withdrawals: Decimal
    net_invested: Decimal
    accounts: list[PlacementAccountResponse]
