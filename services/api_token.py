"""API token service: mint, list, revoke and authenticate machine credentials."""

import hashlib
import hmac
import secrets
import uuid as uuid_lib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from config import get_settings
from models.api_token import ApiToken
from models.user import User
from services.encryption import (
    DecryptionError,
    decrypt_data,
    encrypt_data,
    init_salt,
    token_unwrap_master_key,
    token_wrap_master_key,
)

# Identifiable prefix so the secret is greppable in logs and catchable by
# secret-scanning tools (GitHub, gitleaks) if it ever leaks into a repo.
TOKEN_PREFIX = "cvw_"
TOKEN_ENTROPY_BYTES = 32
READ_SCOPE = "read"
MAX_TOKENS_PER_USER = 20

# Writing last_used_at on every single tool call would turn a read-only MCP
# request into a write. One update per minute is enough to answer "is this token
# still in use?" in the UI.
LAST_USED_REFRESH_SECONDS = 60


@dataclass(frozen=True)
class ApiTokenPrincipal:
    """Everything a request authenticated by an API token is allowed to do.

    Plain values, never ORM instances: the session that authenticated the
    request is closed before the request is served, and a detached ``User`` would
    raise the moment anything touched one of its attributes.
    """

    user_uuid: str
    username: str
    master_key: str
    scopes: frozenset[str]
    token_uuid: str

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes


def generate_api_token() -> str:
    """Generate a fresh API token secret."""
    return f"{TOKEN_PREFIX}{secrets.token_urlsafe(TOKEN_ENTROPY_BYTES)}"


def hash_api_token(token: str) -> str:
    """HMAC-SHA256 an API token with SECRET_KEY for storage and lookup.

    Same reasoning as refresh tokens: the plaintext never touches the database,
    so a dump alone is not replayable.
    """
    settings = get_settings()
    return hmac.new(
        settings.secret_key.encode("utf-8"), token.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def create_api_token(
    session: Session,
    user: User,
    master_key: str,
    name: str,
    scopes: str = READ_SCOPE,
    expires_in_days: int | None = None,
) -> tuple[ApiToken, str]:
    """
    Mint a token for *user* and wrap their Master Key under it.

    Returns:
        (stored record, plaintext token) — the plaintext is the caller's only
        chance to show it; it cannot be recovered afterwards.
    """
    token = generate_api_token()
    salt = init_salt()

    record = ApiToken(
        uuid=str(uuid_lib.uuid4()),
        user_uuid=user.uuid,
        name_enc=encrypt_data(name, master_key),
        token_hash=hash_api_token(token),
        mk_wrapped=token_wrap_master_key(master_key, token, salt),
        mk_salt=salt,
        scopes=scopes,
        expires_at=(
            datetime.now(timezone.utc) + timedelta(days=expires_in_days)
            if expires_in_days
            else None
        ),
    )

    session.add(record)
    session.commit()
    session.refresh(record)

    return record, token


def list_api_tokens(session: Session, user_uuid: str) -> list[ApiToken]:
    """Return the user's tokens that are still live, newest first."""
    tokens = session.exec(
        select(ApiToken).where(
            ApiToken.user_uuid == user_uuid,
            ApiToken.revoked_at == None,  # noqa: E711
        )
    ).all()
    return sorted(tokens, key=lambda t: t.created_at, reverse=True)


def count_active_tokens(session: Session, user_uuid: str) -> int:
    """How many live tokens the user holds, for the per-account cap."""
    return len(list_api_tokens(session, user_uuid))


def decrypt_token_name(record: ApiToken, master_key: str) -> str:
    """Decrypt a token label, degrading to a placeholder rather than raising.

    A label that cannot be read must never hide the token from the list where it
    could be revoked.
    """
    try:
        return decrypt_data(record.name_enc, master_key)
    except Exception:
        # Not just DecryptionError: a truncated or non-Base64 column raises from
        # the decoder before AES-GCM ever runs.
        return "(nom illisible)"


def revoke_api_token(session: Session, user_uuid: str, token_uuid: str) -> bool:
    """
    Revoke one of the user's tokens.

    Returns:
        True if a live token was revoked, False if it does not exist, belongs to
        somebody else, or was already revoked.
    """
    record = session.exec(
        select(ApiToken).where(
            ApiToken.uuid == token_uuid,
            ApiToken.user_uuid == user_uuid,
            ApiToken.revoked_at == None,  # noqa: E711
        )
    ).first()

    if not record:
        return False

    record.revoked_at = datetime.now(timezone.utc)
    session.add(record)
    session.commit()
    return True


def revoke_user_api_tokens(session: Session, user_uuid: str) -> int:
    """Revoke every live token of a user. Returns how many were revoked."""
    tokens = list_api_tokens(session, user_uuid)
    now = datetime.now(timezone.utc)
    for record in tokens:
        record.revoked_at = now
        session.add(record)
    session.commit()
    return len(tokens)


def _as_utc(value: datetime | None) -> datetime | None:
    """Read a timestamp back as timezone-aware UTC.

    SQLite (and any column declared without a timezone) hands back naive
    datetimes; comparing one to an aware ``now()`` raises TypeError.
    """
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _touch_last_used(session: Session, record: ApiToken, now: datetime) -> None:
    """Record the token's use, at most once per LAST_USED_REFRESH_SECONDS."""
    last_used = _as_utc(record.last_used_at)
    if last_used and (now - last_used).total_seconds() < LAST_USED_REFRESH_SECONDS:
        return

    record.last_used_at = now
    session.add(record)
    session.commit()


def authenticate_api_token(session: Session, token: str) -> ApiTokenPrincipal | None:
    """
    Resolve a bearer token into the caller it authorises.

    Returns None — never a reason — for every failure mode, so the transport
    cannot be used to tell "unknown token" from "revoked" or "expired".
    """
    if not token or not token.startswith(TOKEN_PREFIX):
        return None

    record = session.exec(
        select(ApiToken).where(ApiToken.token_hash == hash_api_token(token))
    ).first()

    if not record or record.revoked_at is not None:
        return None

    now = datetime.now(timezone.utc)
    expires_at = _as_utc(record.expires_at)
    if expires_at and expires_at <= now:
        return None

    user = session.get(User, record.user_uuid)
    if not user or not user.is_active:
        return None

    try:
        master_key = token_unwrap_master_key(record.mk_wrapped, token, record.mk_salt)
    except DecryptionError:
        # The hash matched but the wrap did not open: the row is corrupt, or
        # SECRET_KEY rotated under it. Either way the token is unusable.
        return None

    _touch_last_used(session, record, now)

    return ApiTokenPrincipal(
        user_uuid=user.uuid,
        username=user.username,
        master_key=master_key,
        scopes=frozenset(record.scopes.split()),
        token_uuid=record.uuid,
    )
