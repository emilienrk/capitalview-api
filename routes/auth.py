"""Authentication routes."""

import asyncio
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Cookie, Depends, Header, HTTPException, Request, Response, status
from jwt import InvalidTokenError
from sqlmodel import Session, delete, select

from config import get_settings
from database import get_session
from models.user import TotpBackupCode, User
from dtos.auth import (
    Login2FARequest,
    LoginRequest,
    MessageResponse,
    PasswordChangeRequest,
    RecoverRequest,
    RecoverResponse,
    RecoveryKeyGenerateRequest,
    RecoveryKeyResponse,
    RegisterRequest,
    TokenResponse,
    TwoFADisableRequest,
    TwoFAEnableRequest,
    TwoFAEnableResponse,
    TwoFARequiredResponse,
    TwoFASetupRequest,
    TwoFASetupResponse,
    UserResponse,
    EmailUpdateRequest,
    UsernameUpdateRequest,
)
from services.auth import (
    PENDING_2FA_TOKEN_EXPIRE_MINUTES,
    authenticate_user,
    create_access_token,
    create_pending_2fa_token,
    create_refresh_token,
    create_refresh_token_db,
    decode_pending_2fa_token,
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
    server_encrypt,
    unwrap_master_key,
    wrap_master_key,
)
from services.totp import (
    build_otpauth_uri,
    generate_backup_codes,
    generate_recovery_key,
    generate_totp_secret,
    hash_backup_code,
    normalize_recovery_key,
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


def _finalize_login(
    session: Session,
    user: User,
    master_key: str,
    response: Response,
    background_tasks: BackgroundTasks,
    return_key_in_json: bool,
) -> TokenResponse:
    """Issue a full session: tokens, cookies, community refresh, history catchup."""
    settings = get_settings()

    user.last_login = datetime.now(timezone.utc)
    session.add(user)
    session.commit()

    access_token = create_access_token(data={"sub": user.uuid})
    refresh_token_str = create_refresh_token()
    create_refresh_token_db(session, user.uuid, refresh_token_str)
    _set_session_cookies(response, refresh_token_str, master_key)

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


@router.post("/login", response_model=TokenResponse | TwoFARequiredResponse)
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

    If 2FA is enabled, no session is issued yet: the response is
    ``{two_fa_required, pending_token, expires_in}`` and the login must be
    completed with the TOTP code via ``POST /auth/login/2fa``.

    **Security - Master Key in Response:**
    By default, the master_key is only set as an HttpOnly cookie (secure for browsers).
    To also receive it in JSON response (for server-side automation like n8n), add header:
    `X-Return-Master-Key: true`

    Note: Do not use this header from the web frontend — rely on the HttpOnly cookie instead.
    """
    user = authenticate_user(session, payload.email, payload.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    master_key = resolve_master_key(session, user, payload.password)

    if user.totp_enabled:
        # Password OK but no session yet: hand out a short-lived pending token
        return TwoFARequiredResponse(
            pending_token=create_pending_2fa_token(user.uuid, master_key),
            expires_in=PENDING_2FA_TOKEN_EXPIRE_MINUTES * 60,
        )

    # Only return master_key in JSON if explicitly requested (opt-in for security)
    return_key_in_json = bool(x_return_master_key and x_return_master_key.lower() in ("true", "1", "yes"))
    return _finalize_login(session, user, master_key, response, background_tasks, return_key_in_json)


@router.post("/login/2fa", response_model=TokenResponse)
async def login_2fa(
    request: Request,
    payload: Login2FARequest,
    response: Response,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    x_return_master_key: Annotated[str | None, Header(alias="X-Return-Master-Key")] = None
):
    """
    Login step 2: validate the TOTP code (or a backup code) and issue the session.
    """
    await _check_rate_limit(request, "login_2fa", max_calls=10, window_seconds=60)

    invalid_token = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Session de connexion expirée, veuillez vous reconnecter"
    )
    try:
        user_uuid, master_key = decode_pending_2fa_token(payload.pending_token)
    except InvalidTokenError:
        raise invalid_token

    user = session.get(User, user_uuid)
    if not user or not user.is_active or not user.totp_enabled:
        raise invalid_token

    if not verify_second_factor(session, user, payload.code):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Code de vérification 2FA invalide"
        )

    return_key_in_json = bool(x_return_master_key and x_return_master_key.lower() in ("true", "1", "yes"))
    return _finalize_login(session, user, master_key, response, background_tasks, return_key_in_json)


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


@router.post("/recovery-key", response_model=RecoveryKeyResponse)
async def generate_account_recovery_key(
    request: Request,
    payload: RecoveryKeyGenerateRequest,
    current_user: Annotated[User, Depends(get_current_active_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session)
):
    """
    Generate (or regenerate) the account recovery key.

    The recovery key wraps the same Master Key as the password, so it can
    restore access — and the encrypted data — if the password is forgotten.
    It is returned ONCE and never stored in plaintext: write it down.
    """
    await _check_rate_limit(request, "recovery_key", max_calls=10, window_seconds=3600)

    if not verify_password(payload.password, current_user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Mot de passe incorrect"
        )

    recovery_key = generate_recovery_key()
    salt = init_salt()
    current_user.mk_wrapped_recovery = wrap_master_key(
        master_key, normalize_recovery_key(recovery_key), salt
    )
    current_user.mk_salt_recovery = salt
    session.add(current_user)
    session.commit()

    return RecoveryKeyResponse(recovery_key=recovery_key)


@router.post("/recover", response_model=RecoverResponse)
async def recover_account(
    request: Request,
    payload: RecoverRequest,
    response: Response,
    session: Session = Depends(get_session)
):
    """
    Reset the password using the recovery key (no SMTP required).

    Unwraps the Master Key with the recovery key, re-wraps it with the new
    password, and logs the user in. The used recovery key is single-use:
    a replacement key is returned in the response (shown once).
    """
    await _check_rate_limit(request, "recover", max_calls=5, window_seconds=3600)
    settings = get_settings()

    invalid = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Email ou clé de récupération invalide"
    )

    user = session.exec(select(User).where(User.email == payload.email)).first()
    if not user or not user.is_active or not user.mk_wrapped_recovery or not user.mk_salt_recovery:
        raise invalid

    if user.totp_enabled and not verify_second_factor(session, user, payload.totp_code):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Code de vérification 2FA invalide"
        )

    try:
        master_key = unwrap_master_key(
            user.mk_wrapped_recovery,
            normalize_recovery_key(payload.recovery_key),
            user.mk_salt_recovery,
        )
    except DecryptionError:
        raise invalid

    # Re-wrap with the new password
    new_salt = init_salt()
    user.mk_wrapped_password = wrap_master_key(master_key, payload.new_password, new_salt)
    user.mk_salt_password = new_salt
    user.password_hash = hash_password(payload.new_password)

    # The used recovery key is consumed — issue a replacement
    new_recovery_key = generate_recovery_key()
    recovery_salt = init_salt()
    user.mk_wrapped_recovery = wrap_master_key(
        master_key, normalize_recovery_key(new_recovery_key), recovery_salt
    )
    user.mk_salt_recovery = recovery_salt
    user.last_login = datetime.now(timezone.utc)
    session.add(user)
    session.commit()

    revoke_user_refresh_tokens(session, user.uuid)
    refresh_token_str = create_refresh_token()
    create_refresh_token_db(session, user.uuid, refresh_token_str)
    _set_session_cookies(response, refresh_token_str, master_key)

    access_token = create_access_token(data={"sub": user.uuid})
    return RecoverResponse(
        access_token=access_token,
        token_type="bearer",
        expires_in=settings.access_token_expire_minutes * 60,
        new_recovery_key=new_recovery_key
    )


# ===== TWO-FACTOR AUTHENTICATION (TOTP) =====

@router.post("/2fa/setup", response_model=TwoFASetupResponse)
async def setup_2fa(
    request: Request,
    payload: TwoFASetupRequest,
    current_user: Annotated[User, Depends(get_current_active_user)],
    session: Session = Depends(get_session)
):
    """
    Start 2FA setup: generate a TOTP secret (pending until confirmed).

    Returns the secret and the otpauth:// URI to render as a QR code.
    2FA is only activated once a first code is validated via /auth/2fa/enable.
    """
    await _check_rate_limit(request, "2fa_setup", max_calls=10, window_seconds=3600)

    if current_user.totp_enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="La double authentification est déjà activée"
        )

    if not verify_password(payload.password, current_user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Mot de passe incorrect"
        )

    secret = generate_totp_secret()
    current_user.totp_secret_enc = server_encrypt(secret, "totp")
    current_user.totp_last_used_step = None
    session.add(current_user)
    session.commit()

    return TwoFASetupResponse(
        secret=secret,
        otpauth_uri=build_otpauth_uri(secret, current_user.email),
    )


@router.post("/2fa/enable", response_model=TwoFAEnableResponse)
async def enable_2fa(
    request: Request,
    payload: TwoFAEnableRequest,
    current_user: Annotated[User, Depends(get_current_active_user)],
    session: Session = Depends(get_session)
):
    """
    Confirm 2FA activation with a first valid TOTP code.

    Returns 10 single-use backup codes — shown once, store them safely.
    """
    await _check_rate_limit(request, "2fa_enable", max_calls=10, window_seconds=3600)

    if current_user.totp_enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="La double authentification est déjà activée"
        )
    if not current_user.totp_secret_enc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Aucune configuration 2FA en attente. Appelez d'abord /auth/2fa/setup"
        )

    if not verify_second_factor(session, current_user, payload.code):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Code de vérification 2FA invalide"
        )

    current_user.totp_enabled = True
    session.add(current_user)

    # Replace any previous backup codes
    session.exec(delete(TotpBackupCode).where(TotpBackupCode.user_uuid == current_user.uuid))
    backup_codes = generate_backup_codes()
    for code in backup_codes:
        session.add(TotpBackupCode(user_uuid=current_user.uuid, code_hash=hash_backup_code(code)))
    session.commit()

    return TwoFAEnableResponse(backup_codes=backup_codes)


@router.post("/2fa/disable", response_model=MessageResponse)
async def disable_2fa(
    request: Request,
    payload: TwoFADisableRequest,
    current_user: Annotated[User, Depends(get_current_active_user)],
    session: Session = Depends(get_session)
):
    """
    Disable 2FA. Requires the password AND a valid TOTP/backup code.
    """
    await _check_rate_limit(request, "2fa_disable", max_calls=10, window_seconds=3600)

    if not current_user.totp_enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="La double authentification n'est pas activée"
        )

    if not verify_password(payload.password, current_user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Mot de passe incorrect"
        )

    if not verify_second_factor(session, current_user, payload.code):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Code de vérification 2FA invalide"
        )

    current_user.totp_enabled = False
    current_user.totp_secret_enc = None
    current_user.totp_last_used_step = None
    session.add(current_user)
    session.exec(delete(TotpBackupCode).where(TotpBackupCode.user_uuid == current_user.uuid))
    session.commit()

    return MessageResponse(message="Double authentification désactivée")


@router.post("/2fa/backup-codes", response_model=TwoFAEnableResponse)
async def regenerate_backup_codes(
    request: Request,
    payload: TwoFADisableRequest,
    current_user: Annotated[User, Depends(get_current_active_user)],
    session: Session = Depends(get_session)
):
    """
    Regenerate the 10 single-use backup codes (invalidates the previous ones).
    Requires the password AND a valid TOTP/backup code.
    """
    await _check_rate_limit(request, "2fa_backup_codes", max_calls=10, window_seconds=3600)

    if not current_user.totp_enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="La double authentification n'est pas activée"
        )

    if not verify_password(payload.password, current_user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Mot de passe incorrect"
        )

    if not verify_second_factor(session, current_user, payload.code):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Code de vérification 2FA invalide"
        )

    session.exec(delete(TotpBackupCode).where(TotpBackupCode.user_uuid == current_user.uuid))
    backup_codes = generate_backup_codes()
    for code in backup_codes:
        session.add(TotpBackupCode(user_uuid=current_user.uuid, code_hash=hash_backup_code(code)))
    session.commit()

    return TwoFAEnableResponse(backup_codes=backup_codes)
