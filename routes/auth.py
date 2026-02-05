"""Authentication routes."""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlmodel import Session, select

from config import get_settings
from database import get_session
from models.user import User
from schemas.auth import (
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
    hash_password,
    revoke_refresh_token,
    revoke_user_refresh_tokens,
    verify_refresh_token,
)


router = APIRouter(prefix="/auth", tags=["Authentication"])


def rate_limit_key_func(request: Request):
    """Skip rate limiting for OPTIONS requests."""
    if request.method == "OPTIONS":
        return None
    return get_remote_address(request)


limiter = Limiter(key_func=rate_limit_key_func)


@router.post("/register", response_model=MessageResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit("10/hour")
def register(
    request: Request,
    payload: RegisterRequest,
    session: Session = Depends(get_session)
):
    """
    Register a new user.
    
    - **username**: Username (3-50 characters)
    - **email**: Valid email address
    - **password**: Password (minimum 8 characters)
    """
    # Check if email or username already exists
    existing_user = session.exec(select(User).where(
        (User.email == payload.email) | (User.username == payload.username)
    )).first()
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Un compte avec cet email ou ce nom d'utilisateur existe déjà"
        )
    
    # Create user
    user = User(
        username=payload.username,
        email=payload.email,
        password_hash=hash_password(payload.password)
    )
    
    session.add(user)
    session.commit()
    session.refresh(user)
    
    return MessageResponse(message="User registered successfully")


@router.post("/login", response_model=TokenResponse)
@limiter.limit("5/minute")
def login(
    request: Request,
    payload: LoginRequest,
    response: Response,
    session: Session = Depends(get_session)
):
    """
    Login with email and password.
    
    Returns access token (15 min). Refresh token stored in HttpOnly cookie.
    """
    settings = get_settings()
    
    # Authenticate user
    user = authenticate_user(session, payload.email, payload.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # Create tokens
    access_token = create_access_token(
        data={"sub": str(user.id)}
    )
    refresh_token_str = create_refresh_token()
    
    # Store refresh token in database
    create_refresh_token_db(session, user.id, refresh_token_str)
    
    # Set refresh token as HttpOnly cookie
    response.set_cookie(
        key="refresh_token",
        value=refresh_token_str,
        httponly=True,
        secure=settings.environment != "dev",
        samesite="strict",
        path="/auth",
        max_age=settings.refresh_token_expire_days * 86400
    )
    
    return TokenResponse(
        access_token=access_token,
        token_type="bearer",
        expires_in=settings.access_token_expire_minutes * 60
    )


@router.post("/refresh", response_model=TokenResponse)
@limiter.limit("10/minute")
def refresh_token(
    request: Request,
    response: Response,
    refresh_token: str = Cookie(None),
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
    
    # Verify refresh token
    token_record = verify_refresh_token(session, refresh_token)
    if not token_record:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token"
        )
    
    # Get user
    user = session.get(User, token_record.user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    
    # Rotate refresh token
    revoke_refresh_token(session, refresh_token)
    new_refresh_token = create_refresh_token()
    create_refresh_token_db(session, user.id, new_refresh_token)
    
    # Update cookie with new refresh token
    response.set_cookie(
        key="refresh_token",
        value=new_refresh_token,
        httponly=True,
        secure=settings.environment != "dev",
        samesite="strict",
        max_age=settings.refresh_token_expire_days * 86400,
        path="/auth",
    )
    
    # Create new access token
    access_token = create_access_token(
        data={"sub": str(user.id)}
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
    Logout current user by revoking all refresh tokens and clearing cookie.
    
    Requires authentication.
    """
    settings = get_settings()
    revoke_user_refresh_tokens(session, current_user.id)

    response.delete_cookie(
        key="refresh_token",
        httponly=True,
        secure=get_settings().environment != "dev",
        samesite="strict",
        path="/auth",
    )
    
    return MessageResponse(
        message="Logged out successfully"
    )
    
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
