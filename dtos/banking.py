"""Enable Banking connection schemas (BYO application_id + private key)."""

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


class BankAccountLinkRequest(BaseModel):
    """Body of POST /banking/sessions/{uuid}/link — rattachement to a CapitalView account."""
    identification_hash: str
    bank_account_uuid: str


class BankAccountLinkResult(BaseModel):
    """Response of POST /banking/sessions/{uuid}/link."""
    bank_account_uuid: str
    identification_hash: str
    reconnected: bool


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
    # Several accounts are linked and none carries a recognised card marker, so
    # the sync order, the third reconciliation outcome and R19's "no curve" rule
    # all lost the signal they depend on. Task 12 settles the field against a
    # real session payload.
    card_marker_missing: bool = False
    detail: str | None = None


class BankSyncResponse(BaseModel):
    """Response of POST /banking/sync. The front re-reads the accounts payload
    afterwards rather than depending on this shape (ruling R16)."""
    synced: int
    results: list[BankAccountSyncResult]
