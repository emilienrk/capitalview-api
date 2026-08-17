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
