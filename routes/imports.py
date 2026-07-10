"""Unified platform-import routes: list sources, detect format, preview, confirm."""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlmodel import Session

from database import get_session
from dtos.crypto import FIAT_ASSET_KEYS
from dtos.imports import (
    DetectRequest,
    DetectResponse,
    ImportConfirmRequest,
    ImportConfirmResponse,
    ImportPreviewRequest,
    ImportPreviewResponse,
    ImportSourcesResponse,
)
from models.enums import AssetType
from models.user import User
from services.account_history import trigger_post_transaction_updates
from services.auth import get_current_active_user, get_master_key
from services.bank import get_bank_account
from services.crypto_account import get_crypto_account
from services.imports.base import MAX_CSV_BYTES, MAX_CSV_ROWS, ImportCategory, ImportParser
from services.imports.registry import detect_source, get_parser, list_parsers
from services.stock_account import get_stock_account

router = APIRouter(prefix="/imports", tags=["Imports"])


def _check_csv_size(csv_content: str) -> None:
    if len(csv_content.encode("utf-8", errors="ignore")) > MAX_CSV_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="Fichier CSV trop volumineux (5 Mo maximum)",
        )
    if csv_content.count("\n") + 1 > MAX_CSV_ROWS:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="Fichier CSV trop long (20 000 lignes maximum)",
        )


def _get_parser_or_404(source_id: str) -> ImportParser:
    parser = get_parser(source_id)
    if parser is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Source d'import inconnue: {source_id}",
        )
    return parser


def _check_account_ownership(
    parser: ImportParser,
    session: Session,
    account_id: str,
    user_uuid: str,
    master_key: str,
) -> None:
    """404 when the target account does not exist or belongs to another user."""
    getters = {
        ImportCategory.CRYPTO: get_crypto_account,
        ImportCategory.STOCK: get_stock_account,
        ImportCategory.BANK: get_bank_account,
    }
    account = getters[parser.category](session, account_id, user_uuid, master_key)
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")


@router.get("/sources", response_model=ImportSourcesResponse)
def get_import_sources(
    current_user: Annotated[User, Depends(get_current_active_user)],
):
    """List every available import source (parser) with its category and hints."""
    return ImportSourcesResponse(sources=list_parsers())


@router.post("/detect", response_model=DetectResponse)
def detect_import_source(
    data: DetectRequest,
    current_user: Annotated[User, Depends(get_current_active_user)],
):
    """Score the CSV headers against every parser (best matches first)."""
    _check_csv_size(data.csv_content)
    return DetectResponse(matches=detect_source(data.csv_content))


@router.post("/{source_id}/preview", response_model=ImportPreviewResponse)
def preview_import(
    source_id: str,
    data: ImportPreviewRequest,
    current_user: Annotated[User, Depends(get_current_active_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """
    Parse the CSV with the given source parser and return a reviewable preview.

    Passing ``account_id`` enables duplicate detection against the target
    account (rows/groups already imported are flagged ``is_duplicate``).
    """
    _check_csv_size(data.csv_content)
    parser = _get_parser_or_404(source_id)

    if data.account_id:
        _check_account_ownership(parser, session, data.account_id, current_user.uuid, master_key)

    return parser.preview(
        session,
        data.csv_content,
        data.options,
        account_id=data.account_id,
        master_key=master_key if data.account_id else None,
    )


@router.post("/{source_id}/confirm", response_model=ImportConfirmResponse, status_code=201)
def confirm_import(
    source_id: str,
    data: ImportConfirmRequest,
    background_tasks: BackgroundTasks,
    current_user: Annotated[User, Depends(get_current_active_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """
    Create the transactions (or history points) from a confirmed preview.

    Duplicate rows are skipped by default (``skip_duplicates``); fingerprints
    are recomputed server-side, client flags are never trusted.
    """
    parser = _get_parser_or_404(source_id)
    _check_account_ownership(parser, session, data.account_id, current_user.uuid, master_key)

    result = parser.execute(session, data.account_id, data, master_key)

    # Rebuild account history snapshots for transaction-based categories.
    # Bank imports write snapshots directly — nothing to trigger.
    if parser.category in (ImportCategory.CRYPTO, ImportCategory.STOCK):
        past_dates: list[date] = []
        affected_assets: set[str] = set()

        for g in data.crypto_groups or []:
            try:
                past_dates.append(date.fromisoformat(g.timestamp[:10]))
            except ValueError:
                pass
            for row in g.rows:
                if row.mapped_asset_key and row.mapped_asset_key not in FIAT_ASSET_KEYS:
                    affected_assets.add(row.mapped_asset_key)

        for row in data.stock_rows or []:
            try:
                past_dates.append(date.fromisoformat(row.executed_at[:10]))
            except ValueError:
                pass
            if row.asset_key and row.asset_key not in FIAT_ASSET_KEYS:
                affected_assets.add(row.asset_key)

        trigger_post_transaction_updates(
            session=session,
            background_tasks=background_tasks,
            user_uuid=current_user.uuid,
            master_key=master_key,
            account_id=data.account_id,
            asset_type=AssetType.CRYPTO if parser.category == ImportCategory.CRYPTO else AssetType.STOCK,
            affected_dates=past_dates,
            affected_assets=list(affected_assets),
        )

    return result
