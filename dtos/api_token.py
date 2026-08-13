"""API token schemas for requests and responses."""

from datetime import datetime

from pydantic import BaseModel, Field


class ApiTokenCreateRequest(BaseModel):
    """Mint a new API token. Re-authentication is required, as for any secret."""

    password: str
    totp_code: str | None = Field(
        default=None, description="Required when 2FA is enabled on the account"
    )
    name: str = Field(..., min_length=1, max_length=60, description="Label, e.g. 'Claude Desktop'")
    expires_in_days: int | None = Field(
        default=None, ge=1, le=365, description="None = no expiry"
    )


class ApiTokenResponse(BaseModel):
    """A token as listed in the UI. Never carries the secret."""

    uuid: str
    name: str
    scopes: list[str]
    created_at: datetime
    last_used_at: datetime | None = None
    expires_at: datetime | None = None


class ApiTokenCreatedResponse(ApiTokenResponse):
    """The mint response — the only time the plaintext token is ever returned."""

    token: str = Field(..., description="Shown once. Not recoverable afterwards.")


class McpConnectionResponse(BaseModel):
    """Everything needed to point an MCP client at this deployment."""

    url: str
    transport: str = "streamable-http"
    enabled: bool
