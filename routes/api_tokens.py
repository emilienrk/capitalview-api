"""API token routes: mint, list and revoke machine credentials."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlmodel import Session

from config import get_settings
from database import get_session
from dtos.api_token import (
    ApiTokenCreatedResponse,
    ApiTokenCreateRequest,
    ApiTokenResponse,
    McpConnectionResponse,
)
from models.user import User
from routes.auth import _check_rate_limit
from services.api_token import (
    MAX_TOKENS_PER_USER,
    READ_SCOPE,
    count_active_tokens,
    create_api_token,
    decrypt_token_name,
    list_api_tokens,
    revoke_api_token,
)
from services.auth import (
    get_current_active_user,
    get_master_key,
    verify_password,
    verify_second_factor,
)

router = APIRouter(prefix="/auth/tokens", tags=["API Tokens"])


@router.get("/mcp", response_model=McpConnectionResponse)
def get_mcp_connection(
    current_user: Annotated[User, Depends(get_current_active_user)],
):
    """Where to point an MCP client, so the UI never has to hardcode the URL."""
    settings = get_settings()
    return McpConnectionResponse(url=settings.mcp_public_url, enabled=settings.mcp_enabled)


@router.get("", response_model=list[ApiTokenResponse])
def list_tokens(
    current_user: Annotated[User, Depends(get_current_active_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """List the account's live API tokens. Secrets are never returned."""
    return [
        ApiTokenResponse(
            uuid=record.uuid,
            name=decrypt_token_name(record, master_key),
            scopes=record.scopes.split(),
            created_at=record.created_at,
            last_used_at=record.last_used_at,
            expires_at=record.expires_at,
        )
        for record in list_api_tokens(session, current_user.uuid)
    ]


@router.post("", response_model=ApiTokenCreatedResponse, status_code=status.HTTP_201_CREATED)
async def create_token(
    request: Request,
    payload: ApiTokenCreateRequest,
    current_user: Annotated[User, Depends(get_current_active_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """
    Mint an API token, wrapping the Master Key under it.

    The token grants standing read access to every figure in the account, so it
    is gated like the recovery key: password, plus a 2FA code when the account
    has 2FA on. It is returned exactly once.
    """
    await _check_rate_limit(request, "api_token_create", max_calls=10, window_seconds=3600)

    if not verify_password(payload.password, current_user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Mot de passe incorrect",
        )

    if current_user.totp_enabled and not verify_second_factor(session, current_user, payload.totp_code):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Code de vérification 2FA invalide",
        )

    if count_active_tokens(session, current_user.uuid) >= MAX_TOKENS_PER_USER:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Limite de {MAX_TOKENS_PER_USER} tokens actifs atteinte. Révoquez-en un d'abord.",
        )

    record, token = create_api_token(
        session,
        current_user,
        master_key,
        name=payload.name.strip(),
        scopes=READ_SCOPE,
        expires_in_days=payload.expires_in_days,
    )

    return ApiTokenCreatedResponse(
        uuid=record.uuid,
        name=payload.name.strip(),
        scopes=record.scopes.split(),
        created_at=record.created_at,
        last_used_at=record.last_used_at,
        expires_at=record.expires_at,
        token=token,
    )


@router.delete("/{token_uuid}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_token(
    token_uuid: str,
    current_user: Annotated[User, Depends(get_current_active_user)],
    session: Session = Depends(get_session),
):
    """Revoke a token. Takes effect on the next MCP call — nothing is cached."""
    if not revoke_api_token(session, current_user.uuid, token_uuid):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Token introuvable",
        )
