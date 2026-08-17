"""Enable Banking connection schemas (BYO application_id + private key)."""

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
