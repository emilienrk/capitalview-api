"""API tokens: long-lived credentials for machine clients (MCP, scripts)."""

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import TEXT, Column
from sqlmodel import Field, SQLModel


class ApiToken(SQLModel, table=True):
    """
    A named, revocable credential that lets a non-browser client call the API.

    Unlike a refresh token, this one also carries data access: the user's Master
    Key is wrapped with a KEK derived from the token itself (``mk_wrapped``), the
    same trick the account recovery key uses. Without it a bearer could
    authenticate but read nothing, since every user record is encrypted under the
    Master Key and the server never stores it in the clear.

    The token itself is never persisted — only its HMAC — so a database leak
    cannot be replayed, and cannot unwrap the Master Key either.
    """

    __tablename__ = "api_tokens"
    __table_args__ = {"extend_existing": True}

    uuid: str = Field(default=None, primary_key=True)
    user_uuid: str = Field(
        sa_column=Column(
            sa.String,
            sa.ForeignKey("users.uuid", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    # User-supplied label ("Claude Desktop"), encrypted like every other user string
    name_enc: str = Field(sa_column=Column(TEXT, nullable=False))
    token_hash: str = Field(nullable=False, unique=True, index=True)
    # Enc(KEK(token), MK) — the only reason a token can read encrypted data
    mk_wrapped: str = Field(sa_column=Column(TEXT, nullable=False))
    mk_salt: str = Field(sa_column=Column(TEXT, nullable=False))
    # Space-separated scope list. Only "read" is issued today; the column exists
    # so that write scopes can be added without a migration.
    scopes: str = Field(default="read", nullable=False)
    last_used_at: datetime | None = Field(default=None)
    expires_at: datetime | None = Field(default=None)
    revoked_at: datetime | None = Field(default=None)
    created_at: datetime = Field(
        default=sa.func.now(),
        sa_column=Column(sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
