"""Authentication service for JWT and password management."""

import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated

import jwt
import nacl.pwhash
import nacl.exceptions
from jwt import InvalidTokenError
from fastapi import Depends, HTTPException, status, Header, Cookie
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlmodel import Session, select

from config import get_settings
from database import get_session
from models.user import RefreshToken, TotpBackupCode, User
from services.encryption import (
    get_masterkey,
    init_salt,
    server_decrypt,
    server_encrypt,
    unwrap_master_key,
    wrap_master_key,
)
from services.totp import hash_backup_code, verify_totp

PENDING_2FA_TOKEN_EXPIRE_MINUTES = 5


def verify_password(plain_password: str, password_hash: str) -> bool:
    """
    Verify a password against an Argon2id hash.
    
    Args:
        plain_password: Plain text password
        password_hash: PHC formatted hash string
        
    Returns:
        True if matches, False otherwise
    """
    try:
        nacl.pwhash.verify(password_hash.encode('utf-8'), plain_password.encode('utf-8'))
        return True
    except nacl.exceptions.InvalidkeyError:
        return False


def resolve_master_key(session: Session, user: User, password: str) -> str:
    """
    Resolve the user's Master Key at login time.

    Wrapped accounts: derive the KEK from the password and unwrap the stored MK.
    Legacy accounts (no wrapped MK yet): derive the MK directly from the
    password as before, then wrap it so the account is migrated transparently.
    The MK itself never changes — no data re-encryption is ever needed.
    """
    if user.mk_wrapped_password and user.mk_salt_password:
        return unwrap_master_key(user.mk_wrapped_password, password, user.mk_salt_password)

    master_key = get_masterkey(password, user.auth_salt)
    try:
        salt = init_salt()
        user.mk_wrapped_password = wrap_master_key(master_key, password, salt)
        user.mk_salt_password = salt
        session.add(user)
        session.commit()
    except Exception:
        # Migration must never block a legacy login — retry at next login
        session.rollback()
    return master_key


def verify_second_factor(session: Session, user: User, code: str | None) -> bool:
    """
    Verify a second factor for a 2FA-enabled user: a TOTP code (with replay
    protection — each time step is accepted only once) or a single-use backup code.
    """
    if not code:
        return False

    # TOTP codes are exactly 6 digits; backup codes are 10 Base32 chars
    if user.totp_secret_enc:
        secret = server_decrypt(user.totp_secret_enc, "totp")
        step = verify_totp(secret, code)
        if step is not None:
            if user.totp_last_used_step is not None and step <= user.totp_last_used_step:
                return False
            user.totp_last_used_step = step
            session.add(user)
            session.commit()
            return True

    record = session.exec(
        select(TotpBackupCode).where(
            TotpBackupCode.user_uuid == user.uuid,
            TotpBackupCode.code_hash == hash_backup_code(code),
            TotpBackupCode.used_at == None,  # noqa: E711
        )
    ).first()
    if record:
        record.used_at = datetime.now(timezone.utc)
        session.add(record)
        session.commit()
        return True

    return False


def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    """
    Create a JWT access token.
    
    Args:
        data: Dictionary containing the claims to encode
        expires_delta: Optional expiration time delta
        
    Returns:
        Encoded JWT token string
    """
    settings = get_settings()
    to_encode = data.copy()
    
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_expire_minutes)
    
    to_encode.update({
        "exp": expire,
        "type": "access",
        "iat": datetime.now(timezone.utc)
    })
    
    encoded_jwt = jwt.encode(to_encode, settings.secret_key, algorithm=settings.algorithm)
    return encoded_jwt


def create_refresh_token() -> str:
    """Generate a secure random refresh token."""
    return secrets.token_urlsafe(32)


def create_pending_2fa_token(user_uuid: str, master_key: str) -> str:
    """
    Create the short-lived token issued between login step 1 (password OK)
    and step 2 (TOTP code). It is NOT an access token: its only use is
    POST /auth/login/2fa. The Master Key travels inside it encrypted with a
    server key (claim ``mkc``), so the bearer cannot read it.
    """
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_uuid,
        "type": "2fa_pending",
        "mkc": server_encrypt(master_key, "2fa-pending"),
        "iat": now,
        "exp": now + timedelta(minutes=PENDING_2FA_TOKEN_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)


def decode_pending_2fa_token(token: str) -> tuple[str, str]:
    """
    Decode a pending 2FA token.

    Returns:
        (user_uuid, master_key)

    Raises:
        InvalidTokenError: if the token is invalid, expired or of the wrong type.
    """
    settings = get_settings()
    payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
    if payload.get("type") != "2fa_pending":
        raise InvalidTokenError("Invalid token type")
    user_uuid = payload.get("sub")
    mkc = payload.get("mkc")
    if not user_uuid or not mkc:
        raise InvalidTokenError("Missing claims")
    return user_uuid, server_decrypt(mkc, "2fa-pending")


def hash_refresh_token(token: str) -> str:
    """HMAC-SHA256 a refresh token with SECRET_KEY for storage/lookup.

    Refresh tokens are stored hashed (never in plaintext) so that a database
    leak alone cannot be replayed as a valid session.
    """
    settings = get_settings()
    return hmac.new(
        settings.secret_key.encode("utf-8"), token.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def decode_access_token(token: str) -> dict:
    """
    Decode and validate a JWT access token.
    
    Args:
        token: JWT token string
        
    Returns:
        Dictionary containing the token payload
        
    Raises:
        InvalidTokenError: If token is invalid or expired
    """
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
        
        # Verify token type
        if payload.get("type") != "access":
            raise InvalidTokenError("Invalid token type")
            
        return payload
    except InvalidTokenError:
        raise


def authenticate_user(session: Session, email: str, password: str) -> User | None:
    """
    Authenticate a user by email and password.
    
    Args:
        session: Database session
        email: User email
        password: Plain password
        
    Returns:
        User object if authentication successful, None otherwise
    """
    user = session.exec(select(User).where(User.email == email)).first()
    
    if not user:
        return None
    
    if not user.is_active:
        return None
    
    if not verify_password(password, user.password_hash):
        return None
    
    return user


def create_refresh_token_db(
    session: Session,
    user_uuid: str,
    token: str,
    expires_delta: timedelta | None = None
) -> RefreshToken:
    """
    Create and store a refresh token in the database.
    
    Args:
        session: Database session
        user_uuid: User UUID
        token: Refresh token string
        expires_delta: Optional expiration time delta
        
    Returns:
        RefreshToken database object
    """
    settings = get_settings()
    
    if expires_delta:
        expires_at = datetime.now(timezone.utc) + expires_delta
    else:
        expires_at = datetime.now(timezone.utc) + timedelta(days=settings.refresh_token_expire_days)
    
    refresh_token = RefreshToken(
        user_uuid=user_uuid,
        token_hash=hash_refresh_token(token),
        expires_at=expires_at
    )
    
    session.add(refresh_token)
    session.commit()
    session.refresh(refresh_token)
    
    return refresh_token


def verify_refresh_token(session: Session, token: str) -> RefreshToken | None:
    """
    Verify a refresh token is valid and not expired/revoked.
    
    Args:
        session: Database session
        token: Refresh token string
        
    Returns:
        RefreshToken object if valid, None otherwise
    """
    token_record = session.exec(
        select(RefreshToken).where(
            RefreshToken.token_hash == hash_refresh_token(token),
            RefreshToken.revoked == False,  # noqa: E712
            RefreshToken.expires_at > datetime.now(timezone.utc)
        )
    ).first()

    return token_record


def revoke_user_refresh_tokens(session: Session, user_uuid: str) -> int:
    """
    Revoke all refresh tokens for a user.
    
    Args:
        session: Database session
        user_uuid: User UUID
        
    Returns:
        Number of tokens revoked
    """
    tokens = session.exec(
        select(RefreshToken).where(
            RefreshToken.user_uuid == user_uuid,
            RefreshToken.revoked == False  # noqa: E712
        )
    ).all()
    
    count = 0
    for token in tokens:
        token.revoked = True
        count += 1
    
    session.commit()
    return count


def revoke_refresh_token(session: Session, token: str) -> bool:
    """
    Revoke a specific refresh token.
    
    Args:
        session: Database session
        token: Refresh token string
        
    Returns:
        True if token was revoked, False if not found
    """
    token_record = session.exec(
        select(RefreshToken).where(RefreshToken.token_hash == hash_refresh_token(token))
    ).first()
    
    if not token_record:
        return False
    
    token_record.revoked = True
    session.commit()
    return True


security = HTTPBearer()


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(security)],
    session: Session = Depends(get_session)
) -> User:
    """
    Get current authenticated user from JWT token.
    
    This dependency validates the JWT token and returns the User object.
    Use this as a dependency in protected routes.
    
    Args:
        credentials: HTTP Bearer token credentials
        session: Database session
        
    Returns:
        User object
        
    Raises:
        HTTPException: 401 if token is invalid or user not found
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    
    try:
        # Decode JWT token
        payload = decode_access_token(credentials.credentials)
        user_uuid: str = payload.get("sub")
        
        if user_uuid is None:
            raise credentials_exception
            
    except InvalidTokenError:
        raise credentials_exception
    
    # Get user from database (by UUID string)
    user = session.get(User, user_uuid)
    if user is None:
        raise credentials_exception
    
    return user


def get_current_active_user(
    current_user: Annotated[User, Depends(get_current_user)]
) -> User:
    """
    Get current active user. Rejects inactive/disabled accounts.
    """
    if not current_user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is disabled",
        )
    return current_user


def get_master_key(
    master_key_cookie: Annotated[str | None, Cookie(alias="master_key")] = None,
    x_master_key: Annotated[str | None, Header(alias="X-Master-Key")] = None
) -> str:
    """
    FastAPI dependency: extract the Master Key from request.

    Supports two transport modes (cookie takes priority):
      1. **Cookie** ``master_key`` – set automatically by the browser after login.
         Best for the web frontend (HttpOnly, Secure).
      2. **Header** ``X-Master-Key`` – for automation clients (n8n, Postman, scripts)
         that cannot rely on browser cookies.

    The master_key value is a Base64-encoded 32-byte key derived from the
    user's password via Argon2id + HKDF at login time.

    Raises:
        HTTPException 400: if neither cookie nor header is present.
        HTTPException 400: if the value is not valid Base64.
    """
    key = master_key_cookie or x_master_key
    if not key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Master Key missing. Please log in again."
        )

    # Basic Base64 format validation
    try:
        decoded = base64.b64decode(key, validate=True)
        if len(decoded) != 32:
            raise ValueError("Invalid key length")
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Master Key format. Expected Base64-encoded 32-byte key."
        )

    return key
