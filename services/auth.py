"""Authentication service for JWT and password management."""

import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated, Optional

import jwt
import nacl.pwhash
import nacl.exceptions
from jwt import InvalidTokenError
from fastapi import Depends, HTTPException, status, Header, Cookie
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlmodel import Session, select

from config import get_settings
from database import get_session
from models.user import RefreshToken, User


# ============== PASSWORD UTILS ==============

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


# ============== JWT UTILS ==============

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
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


def authenticate_user(session: Session, email: str, password: str) -> Optional[User]:
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
    expires_delta: Optional[timedelta] = None
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
        token=token,
        expires_at=expires_at
    )
    
    session.add(refresh_token)
    session.commit()
    session.refresh(refresh_token)
    
    return refresh_token


def verify_refresh_token(session: Session, token: str) -> Optional[RefreshToken]:
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
            RefreshToken.token == token,
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
        select(RefreshToken).where(RefreshToken.token == token)
    ).first()
    
    if not token_record:
        return False
    
    token_record.revoked = True
    session.commit()
    return True


# ============== FASTAPI DEPENDENCIES ==============

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
    Get the Master Key from Cookie or Header.
    """
    key = master_key_cookie or x_master_key
    if not key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Master Key missing. Please log in again."
        )
    return key
