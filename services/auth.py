"""Authentication service for JWT and password management."""

import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import jwt
from jwt import InvalidTokenError
from sqlmodel import Session, select

from config import get_settings
from models.user import RefreshToken, User


def hash_password(password: str) -> str:
    """Hash a plain password using bcrypt."""
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plain password against a hashed password."""
    return bcrypt.checkpw(plain_password.encode('utf-8'), hashed_password.encode('utf-8'))


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
    
    if not verify_password(password, user.password_hash):
        return None
    
    return user


def create_refresh_token_db(
    session: Session,
    user_id: int,
    token: str,
    expires_delta: Optional[timedelta] = None
) -> RefreshToken:
    """
    Create and store a refresh token in the database.
    
    Args:
        session: Database session
        user_id: User ID
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
        user_id=user_id,
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


def revoke_user_refresh_tokens(session: Session, user_id: int) -> int:
    """
    Revoke all refresh tokens for a user.
    
    Args:
        session: Database session
        user_id: User ID
        
    Returns:
        Number of tokens revoked
    """
    tokens = session.exec(
        select(RefreshToken).where(
            RefreshToken.user_id == user_id,
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

from typing import Annotated
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from database import get_session


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
        user_id: str = payload.get("sub")
        
        if user_id is None:
            raise credentials_exception
            
    except InvalidTokenError:
        raise credentials_exception
    
    # Get user from database
    user = session.get(User, int(user_id))
    if user is None:
        raise credentials_exception
    
    return user


def get_current_active_user(
    current_user: Annotated[User, Depends(get_current_user)]
) -> User:
    """
    Get current active user (extension point for user status checks).
    
    Currently returns the user as-is, but can be extended to check
    for disabled/banned users, email verification, etc.
    """
    # Future: check if user.is_active, user.email_verified, etc.
    return current_user
