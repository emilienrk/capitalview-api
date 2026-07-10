"""Authentication routes."""

import asyncio
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Cookie, Depends, Header, HTTPException, Request, Response, status
from sqlmodel import Session, select

from config import get_settings
from database import get_session
from models.user import User
from dtos.auth import (
    LoginRequest,
    MessageResponse,
    PasswordChangeRequest,
    RegisterRequest,
    TokenResponse,
    UserResponse,
    EmailUpdateRequest,
    UsernameUpdateRequest,
)
from services.auth import (
    authenticate_user,
    create_access_token,
    create_refresh_token,
    create_refresh_token_db,
    get_current_active_user,
    get_current_user,
    get_master_key,
    resolve_master_key,
    revoke_refresh_token,
    revoke_user_refresh_tokens,
    verify_password,
    verify_refresh_token,
    verify_second_factor,
)
from services.encryption import (
    DecryptionError,
    generate_random_master_key,
    init_salt,
    hash_password,
    unwrap_master_key,
    wrap_master_key,
)
from services.community import refresh_community_positions
from services.account_history import run_lazy_catchup


router = APIRouter(prefix="/auth", tags=["Authentication"])


_rate_lock = asyncio.Lock()
_rate_hits: dict[str, list[float]] = defaultdict(list)


def _get_client_ip(request: Request) -> str:
    """Return the real client IP for rate-limiting purposes.

    X-Forwarded-For is client-controlled and trivially spoofable, so it is only
    trusted when the app is known to sit behind TRUSTED_PROXY_COUNT trusted
    reverse proxies (each of which appends its own hop). In that case we take
    the n-th hop from the right, i.e. the IP added by the outermost trusted
    proxy. Otherwise we fall back to the socket peer address.
    """
    trusted_hops = get_settings().trusted_proxy_count
    if trusted_hops > 0:
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            hops = [h.strip() for h in forwarded.split(",") if h.strip()]
            if len(hops) >= trusted_hops:
                return hops[-trusted_hops]
    return request.client.host if request.client else "unknown"


async def _check_rate_limit(request: Request, key: str, max_calls: int, window_seconds: int) -> None:
    """
    Sliding-window rate limiter.  Raises HTTP 429 if the caller has exceeded
    *max_calls* requests within the last *window_seconds* seconds.
    Skips OPTIONS (CORS preflight) entirely.
    """
    if request.method == "OPTIONS":
        return

    ip = _get_client_ip(request)
    bucket = f"{ip}:{key}"
    now = time.monotonic()
    cutoff = now - window_seconds

    async with _rate_lock:
        # Prune timestamps outside the sliding window
        hits = [t for t in _rate_hits[bucket] if t > cutoff]
        if not hits:
            # No recent hits: free the key to avoid accumulation of ephemeral IPs
            _rate_hits.pop(bucket, None)
        else:
            _rate_hits[bucket] = hits
        if len(hits) >= max_calls:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Trop de requêtes, veuillez réessayer plus tard.",
                headers={"Retry-After": str(window_seconds)},
            )
        _rate_hits[bucket].append(now)



@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(
    request: Request,
    payload: RegisterRequest,
    response: Response,
    session: Session = Depends(get_session),
    x_return_master_key: Annotated[str | None, Header(alias="X-Return-Master-Key")] = None
):
    await _check_rate_limit(request, "register", max_calls=10, window_seconds=3600)
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
    # New accounts get a random Master Key (decorrelated from the password),
    # stored wrapped by a password-derived KEK. Changing the password later
    # only re-wraps the MK — no data re-encryption.
    master_key = generate_random_master_key()
    mk_salt_password = init_salt()
    mk_wrapped_password = wrap_master_key(master_key, payload.password, mk_salt_password)

    user = User(
        uuid=user_uuid,
        username=payload.username,
        email=payload.email,
        auth_salt=auth_salt,
        password_hash=hashed_password,
        mk_wrapped_password=mk_wrapped_password,
        mk_salt_password=mk_salt_password
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
async def login(
    request: Request,
    payload: LoginRequest,
    response: Response,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    x_return_master_key: Annotated[str | None, Header(alias="X-Return-Master-Key")] = None
):
    await _check_rate_limit(request, "login", max_calls=5, window_seconds=60)
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
    
    master_key = resolve_master_key(session, user, payload.password)

    # Update last login timestamp
    user.last_login = datetime.now(timezone.utc)
    session.add(user)
    session.commit()

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

    # Refresh community positions if the user has an active profile
    try:
        refresh_community_positions(session, user.uuid, master_key)
    except Exception:
        pass  # Non-critical — don't block login if community sync fails

    # Compute missing account history snapshots in the background (never blocks login)
    background_tasks.add_task(run_lazy_catchup, user.uuid, master_key)
    
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
    background_tasks: BackgroundTasks,
    refresh_token: Annotated[str | None, Cookie()] = None,
    master_key_cookie: Annotated[str | None, Cookie(alias="master_key")] = None,
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

    # Trigger account history catchup if master_key cookie is present
    if master_key_cookie:
        background_tasks.add_task(run_lazy_catchup, user.uuid, master_key_cookie)

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
@router.put("/me/email", response_model=UserResponse)
def update_email(
    request: Request,
    payload: EmailUpdateRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Session = Depends(get_session)
):
    """
    Update email address. Only allowed once every 30 days.
    """
    if current_user.email == payload.email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="C'est déjà votre adresse email"
        )

    # Check 30 day limit
    if current_user.last_email_change:
        days_since_change = (datetime.now(timezone.utc) - current_user.last_email_change).days
        if days_since_change < 30:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Vous devez attendre {30 - days_since_change} jours avant de pouvoir rechanger d'email"
            )

    # Check if email taken
    existing = session.exec(select(User).where(User.email == payload.email)).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cet email est déjà utilisé"
        )

    current_user.email = payload.email
    current_user.last_email_change = datetime.now(timezone.utc)
    
    session.add(current_user)
    session.commit()
    session.refresh(current_user)
    
    return current_user

@router.put("/me/username", response_model=UserResponse)
def update_username(
    request: Request,
    payload: UsernameUpdateRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Session = Depends(get_session)
):
    """
    Update username. Only allowed once.
    """
    if current_user.username == payload.username:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="C'est déjà votre nom d'utilisateur"
        )

    # Check 1 time limit
    if current_user.last_username_change:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Vous ne pouvez modifier votre nom d'utilisateur qu'une seule fois."
        )

    # Check if taken
    existing = session.exec(select(User).where(User.username == payload.username)).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Ce nom d'utilisateur est déjà pris"
        )

    current_user.username = payload.username
    current_user.last_username_change = datetime.now(timezone.utc)

    session.add(current_user)
    session.commit()
    session.refresh(current_user)

    return current_user


def _set_session_cookies(response: Response, refresh_token_str: str, master_key: str) -> None:
    """Set the HttpOnly refresh_token and master_key cookies (same policy as login)."""
    settings = get_settings()
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


@router.put("/me/password", response_model=TokenResponse)
async def change_password(
    request: Request,
    payload: PasswordChangeRequest,
    response: Response,
    current_user: Annotated[User, Depends(get_current_active_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """
    Change the current user's password.

    The Master Key never changes — it is simply re-wrapped with a KEK derived
    from the new password, so all encrypted data stays readable. All refresh
    tokens are revoked; a fresh session is returned in the response cookies.

    Requires the current password, and a TOTP/backup code if 2FA is enabled.
    """
    await _check_rate_limit(request, "password_change", max_calls=5, window_seconds=3600)
    settings = get_settings()

    if not verify_password(payload.current_password, current_user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Mot de passe actuel incorrect"
        )

    if current_user.totp_enabled and not verify_second_factor(session, current_user, payload.totp_code):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Code de vérification 2FA invalide"
        )

    # Consistency guard: the MK from the cookie must match the wrapped MK.
    # Re-wrapping a stale/foreign key would make all data unreadable.
    if current_user.mk_wrapped_password and current_user.mk_salt_password:
        try:
            stored_mk = unwrap_master_key(
                current_user.mk_wrapped_password,
                payload.current_password,
                current_user.mk_salt_password,
            )
        except DecryptionError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Mot de passe actuel incorrect"
            )
        if stored_mk != master_key:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Clé de chiffrement invalide. Veuillez vous reconnecter."
            )
    else:
        # Legacy account not yet migrated: wrap on the fly with the current password
        stored_mk = master_key

    new_salt = init_salt()
    current_user.mk_wrapped_password = wrap_master_key(stored_mk, payload.new_password, new_salt)
    current_user.mk_salt_password = new_salt
    current_user.password_hash = hash_password(payload.new_password)
    session.add(current_user)
    session.commit()

    # Invalidate every existing session, then hand back a fresh one
    revoke_user_refresh_tokens(session, current_user.uuid)
    refresh_token_str = create_refresh_token()
    create_refresh_token_db(session, current_user.uuid, refresh_token_str)
    _set_session_cookies(response, refresh_token_str, stored_mk)

    access_token = create_access_token(data={"sub": current_user.uuid})
    return TokenResponse(
        access_token=access_token,
        token_type="bearer",
        expires_in=settings.access_token_expire_minutes * 60
    )
