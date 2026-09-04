"""Enable Banking connection schemas (BYO application_id + private key)."""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel


class BankConnectionUpdate(BaseModel):
    """Update the Enable Banking application_id and/or private key.

    Field absent from the payload = unchanged; empty string = deletion of that
    field. Mirrors AIProviderUpdate / update_ai_provider.
    """
    application_id: str | None = None
    private_key: str | None = None


class BankConnectionStatus(BaseModel):
    """State of a user's Enable Banking connection (read-only).

    The private key is never returned, only whether it is configured.
    """
    has_credentials: bool
    application_id: str | None


# ---------------------------------------------------------------------------
# Linking flow DTOs (spec §C)
# ---------------------------------------------------------------------------


class BankConfigCheck(BaseModel):
    """GET /banking/check — the pre-flight diagnostic (spec §C1).

    One GET /application call tells us, in one shot, whether the key is
    valid, the application is active, and CapitalView's callback URL is
    among the declared redirect URLs.
    """
    configured: bool
    key_valid: bool
    application_active: bool
    callback_url_declared: bool
    callback_url: str
    # SANDBOX or PRODUCTION, straight from GET /application. Matched by NAME:
    # the contract's x-enum-descriptions for Environment are misaligned with
    # their values, exactly as for SessionStatus. `None` when unreachable.
    environment: str | None = None
    error: str | None = None


class AspspSummary(BaseModel):
    """One bank entry from GET /aspsps, trimmed to what the flow needs."""
    name: str
    country: str
    logo: str | None = None
    beta: bool = False
    maximum_consent_validity: int


class BankAuthorizeRequest(BaseModel):
    """Body of POST /banking/authorize — the bank the user picked."""
    aspsp_name: str
    aspsp_country: str


class BankAuthorizeResponse(BaseModel):
    """Response of POST /banking/authorize — where to send the browser next."""
    auth_url: str


class BankSessionAccount(BaseModel):
    """One account discovered in a bank session, for the rattachement step.

    The display fields come from the accounts payload of POST /sessions, kept
    in bank_sessions.accounts_enc: a later GET /sessions/{id} returns only
    uid + identification hashes, so nothing here could be re-read from the API.
    Without them the picker can only show opaque base64 hashes.
    """
    identification_hash: str
    name: str | None = None
    product: str | None = None
    currency: str | None = None
    cash_account_type: str | None = None
    usage: str | None = None
    # IBAN when the bank provides one, otherwise the "other" identification
    # (BBAN and friends) — AccountResource.account_id carries either.
    account_id: str | None = None
    linked: bool
    bank_account_uuid: str | None = None


class BankSessionLinkedAccount(BaseModel):
    """One CapitalView account attached to a bank session."""
    bank_account_uuid: str
    name: str
    last_synced_at: date | None = None


class BankSessionSummary(BaseModel):
    """GET /banking/sessions — one authorization the user has granted.

    Retired sessions stay in the list: their `BankAccountLink`s survive consent
    expiry by design, so the status is what tells the user a reconnection is
    the only thing missing.
    """
    uuid: str
    aspsp_name: str | None = None
    aspsp_country: str | None = None
    status: str
    status_message: str
    active: bool
    consent_valid_until: datetime
    authorized_at: datetime
    accounts: list[BankSessionLinkedAccount] = []


class BankAccountLinkRequest(BaseModel):
    """Body of POST /banking/sessions/{uuid}/link — rattachement to a CapitalView account."""
    identification_hash: str
    bank_account_uuid: str


class BankAccountLinkResult(BaseModel):
    """Response of POST /banking/sessions/{uuid}/link."""
    bank_account_uuid: str
    identification_hash: str
    reconnected: bool


class BankAccountUnlinkResult(BaseModel):
    """Response of DELETE /banking/accounts/{uuid}/link.

    `reseeded_accounts` are the accounts that were being deduplicated against
    the detached one and are now scheduled for a full re-seed: whatever the
    detached account had shadowed can finally be stored on them.
    """
    bank_account_uuid: str
    transactions_deleted: int
    reseeded_accounts: list[str]


# ---------------------------------------------------------------------------
# Synchronisation DTOs (spec §D)
# ---------------------------------------------------------------------------


class BankAccountSyncResult(BaseModel):
    """What one linked account's sync did.

    `status` is the branch the sequence took: `synced`, `skipped_daily_cap`
    (the once-a-day cap, a no-op and never an error), `reconnect_required`
    (the consent is gone; the link is preserved) or `error`.
    """
    bank_account_uuid: str
    status: str
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    # Rows the bank sent without an amount, a direction or a status. Dropped,
    # never fatal — the reconciliation gap is what makes them visible.
    malformed: int = 0
    removed: int = 0
    snapshots_written: int = 0
    reconciliation_gap: Decimal | None = None
    # `reconciled`, `gap` or `not_reconcilable` (ruling R18); None when no check
    # could run yet (the seeding pass has no bank anchor to compare against).
    reconciliation_status: str | None = None
    detail: str | None = None


class BankSyncResponse(BaseModel):
    """Response of POST /banking/sync. The front re-reads the accounts payload
    afterwards rather than depending on this shape (ruling R16)."""
    synced: int
    results: list[BankAccountSyncResult]


# ---------------------------------------------------------------------------
# Observed flows (the real counterpart of the declared cashflows)
# ---------------------------------------------------------------------------


class BankFlowMonth(BaseModel):
    """One calendar month of observed movement, in the response's currency."""
    period: str  # YYYY-MM
    inflow: Decimal
    outflow: Decimal
    net: Decimal
    inflow_count: int = 0
    outflow_count: int = 0


class BankFlowCurrencyTotal(BaseModel):
    """Movements in a currency other than the headline one, reported apart.

    Amounts arrive unconverted and with no exchange rate, so they are never
    folded into the main total.
    """
    currency: str
    inflow: Decimal
    outflow: Decimal


class BankFlowsResponse(BaseModel):
    """GET /banking/flows — what actually moved on the linked accounts."""
    currency: str
    months: list[BankFlowMonth]
    inflow: Decimal
    outflow: Decimal
    net: Decimal
    # Averaged over the months carrying data, not over the requested window.
    monthly_inflow: Decimal
    monthly_outflow: Decimal
    covered_months: int
    account_count: int
    # Named so a total spanning several accounts can be checked at a glance.
    account_names: list[str] = []
    # Movements paired as one transfer between two of the user's own accounts:
    # counted, reported, and kept out of the totals.
    internal_transfers_excluded: int
    internal_transfers_amount: Decimal
    # Not yet booked, so deliberately outside the monthly figures.
    pending_count: int
    pending_inflow: Decimal
    pending_outflow: Decimal
    other_currencies: list[BankFlowCurrencyTotal]


class BankExportImportResult(BaseModel):
    bank_account_uuid: str
    status: str
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    malformed: int = 0
    snapshots_written: int = 0
    detail: str | None = None


class BankExportImportResponse(BaseModel):
    imported_accounts: int
    results: list[BankExportImportResult]
