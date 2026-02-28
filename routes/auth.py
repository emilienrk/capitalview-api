"""Authentication routes."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Request, Response, status
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlmodel import Session, select

from config import get_settings
from database import get_session
from models.user import User
from dtos.auth import (
    LoginRequest,
    MessageResponse,
    RegisterRequest,
    TokenResponse,
    UserResponse,
)
from services.auth import (
    authenticate_user,
    create_access_token,
    create_refresh_token,
    create_refresh_token_db,
    get_current_user,
    revoke_refresh_token,
    revoke_user_refresh_tokens,
    verify_refresh_token,
)
from services.encryption import get_masterkey, init_salt, hash_password


router = APIRouter(prefix="/auth", tags=["Authentication"])


def rate_limit_key_func(request: Request):
    """Skip rate limiting for OPTIONS requests."""
    if request.method == "OPTIONS":
        return None
    return get_remote_address(request)


limiter = Limiter(key_func=rate_limit_key_func)


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit("10/hour")
def register(
    request: Request,
    payload: RegisterRequest,
    response: Response,
    session: Session = Depends(get_session),
    x_return_master_key: Annotated[str | None, Header(alias="X-Return-Master-Key")] = None
):
    """
    Register a new user.
    
    - **username**: Username (3-50 characters)
    - **email**: Valid email address
    - **password**: Password (minimum 8 characters)
    
    **Security - Master Key in Response:**
    By default, the master_key is only set as an HttpOnly cookie (secure for browsers).
    To also receive it in JSON response (for server-side automation like n8n), add header:
    `X-Return-Master-Key: true`
    
    Note: Do not use this header from the web frontend — rely on the HttpOnly cookie instead.
    """
    settings = get_settings()

    existing_user = session.exec(select(User).where(
        (User.email == payload.email) | (User.username == payload.username)
    )).first()
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Un compte avec cet email ou ce nom d'utilisateur existe déjà"
        )
    
    user_uuid = str(uuid.uuid4())
    auth_salt = init_salt()
    hashed_password = hash_password(payload.password)
    master_key = get_masterkey(payload.password, auth_salt)
    
    user = User(
        uuid=user_uuid,
        username=payload.username,
        email=payload.email,
        auth_salt=auth_salt,
        password_hash=hashed_password
    )
    
    session.add(user)
    session.commit()
    session.refresh(user)

    access_token = create_access_token(
        data={"sub": user.uuid}
    )
    refresh_token_str = create_refresh_token()
    
    create_refresh_token_db(session, user.uuid, refresh_token_str)
    
    response.set_cookie(
        key="refresh_token",
        value=refresh_token_str,
        httponly=True,
        secure=settings.environment == "production",
        samesite="lax",
        max_age=settings.refresh_token_expire_days * 86400,
        path="/auth"
    )

    response.set_cookie(
        key="master_key",
        value=master_key,
        httponly=True,
        secure=settings.environment == "production",
        samesite="lax",
        max_age=settings.refresh_token_expire_days * 86400,
        path="/"
    )
    
    # Only return master_key in JSON if explicitly requested (opt-in for security)
    return_key_in_json = x_return_master_key and x_return_master_key.lower() in ("true", "1", "yes")
    
    return TokenResponse(
        access_token=access_token,
        token_type="bearer",
        expires_in=settings.access_token_expire_minutes * 60,
        master_key=master_key if return_key_in_json else None
    )


@router.post("/login", response_model=TokenResponse)
@limiter.limit("5/minute")
def login(
    request: Request,
    payload: LoginRequest,
    response: Response,
    session: Session = Depends(get_session),
    x_return_master_key: Annotated[str | None, Header(alias="X-Return-Master-Key")] = None
):
    """
    Login with email and password.
    
    Returns access token (15 min). Refresh token & Master Key stored in HttpOnly cookies.
    
    **Security - Master Key in Response:**
    By default, the master_key is only set as an HttpOnly cookie (secure for browsers).
    To also receive it in JSON response (for server-side automation like n8n), add header:
    `X-Return-Master-Key: true`
    
    Note: Do not use this header from the web frontend — rely on the HttpOnly cookie instead.
    """
    settings = get_settings()
    
    user = authenticate_user(session, payload.email, payload.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    master_key = get_masterkey(payload.password, user.auth_salt)

    access_token = create_access_token(
        data={"sub": user.uuid}
    )
    refresh_token_str = create_refresh_token()
    
    create_refresh_token_db(session, user.uuid, refresh_token_str)
    
    response.set_cookie(
        key="refresh_token",
        value=refresh_token_str,
        httponly=True,
        secure=settings.environment == "production",
        samesite="lax",
        max_age=settings.refresh_token_expire_days * 86400,
        path="/auth"
    )

    response.set_cookie(
        key="master_key",
        value=master_key,
        httponly=True,
        secure=settings.environment == "production",
        samesite="lax",
        max_age=settings.refresh_token_expire_days * 86400,
        path="/"
    )
    
    # Only return master_key in JSON if explicitly requested (opt-in for security)
    return_key_in_json = x_return_master_key and x_return_master_key.lower() in ("true", "1", "yes")
    
    return TokenResponse(
        access_token=access_token,
        token_type="bearer",
        expires_in=settings.access_token_expire_minutes * 60,
        master_key=master_key if return_key_in_json else None
    )


@router.post("/refresh", response_model=TokenResponse)
def refresh_token_endpoint(
    request: Request,
    response: Response,
    refresh_token: Annotated[str | None, Cookie()] = None,
    session: Session = Depends(get_session)
):
    """
    Refresh access token using HttpOnly cookie.
    
    Returns a new access token and rotates the refresh token.
    """
    if not refresh_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token missing"
        )
    
    settings = get_settings()
    
    token_record = verify_refresh_token(session, refresh_token)
    if not token_record:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token"
        )
    
    user = session.get(User, token_record.user_uuid)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    
    revoke_refresh_token(session, refresh_token)
    new_refresh_token = create_refresh_token()
    create_refresh_token_db(session, user.uuid, new_refresh_token)
    
    response.set_cookie(
        key="refresh_token",
        value=new_refresh_token,
        httponly=True,
        secure=settings.environment == "production",
        samesite="lax",
        max_age=settings.refresh_token_expire_days * 86400,
        path="/auth"
    )
    
    access_token = create_access_token(
        data={"sub": user.uuid}
    )
    
    return TokenResponse(
        access_token=access_token,
        token_type="bearer",
        expires_in=settings.access_token_expire_minutes * 60
    )


@router.post("/logout", response_model=MessageResponse)
def logout(
    current_user: Annotated[User, Depends(get_current_user)],
    response: Response,
    session: Session = Depends(get_session)
):
    """
    Logout current user by revoking all refresh tokens and clearing cookies.
    
    Requires authentication.
    """
    settings = get_settings()
    revoke_user_refresh_tokens(session, current_user.uuid)

    response.delete_cookie(key="refresh_token", path="/auth", secure=settings.environment == "production", samesite="lax")
    response.delete_cookie(key="master_key", path="/", secure=settings.environment == "production", samesite="lax")
    
    return MessageResponse(message="Logged out successfully")


@router.get("/me", response_model=UserResponse)
def get_current_user_info(
    current_user: Annotated[User, Depends(get_current_user)]
):
    """
    Get current authenticated user information.
    
    Requires authentication.
    """
    return current_user