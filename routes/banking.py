"""Enable Banking linking flow routes (spec §C).

GET /banking/callback is the one exception to the usual auth pattern: it's a
raw browser top-level GET navigation coming back from the bank, not an XHR
call from the SPA, so it never carries an Authorization header — only
whatever cookies ride along under SameSite=Lax. It authenticates itself via
`state` instead (see services/banking/linking.py).
"""

import base64
import html
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session

from config import get_settings
from database import get_session
from dtos.banking import (
    AspspSummary,
    BankAccountLinkRequest,
    BankAccountLinkResult,
    BankAuthorizeRequest,
    BankAuthorizeResponse,
    BankConfigCheck,
    BankConnectionStatus,
    BankConnectionUpdate,
    BankExportImportResponse,
    BankSessionAccount,
    BankSessionSummary,
    BankSyncResponse,
)
from models import User
from services.auth import get_current_user, get_master_key
from services.banking.credentials import (
    get_status,
    upsert_connection,
)
from services.banking.export_import import import_enablebanking_export
from services.banking.errors import BankingApiError
from services.banking.linking import (
    AccountNotFoundInSessionError,
    AspspNotFoundError,
    BankSessionNotFoundError,
    NotConfiguredError,
    TargetAccountNotFoundError,
    check_configuration,
    delete_bank_session,
    handle_callback,
    link_account,
    list_aspsps_for_country,
    list_bank_sessions,
    list_session_accounts,
    start_authorization_flow,
)
from services.banking.sync import sync_user_accounts
from services.settings import get_or_create_settings

router = APIRouter(prefix="/banking", tags=["Banking"])


def require_open_banking(
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
) -> None:
    """Refuse anything that starts, extends or feeds a bank connection when the
    user has not opted in.

    Deliberately not applied to the read-only routes, to DELETE /sessions or to
    the callback: turning the feature back off must leave the user able to see
    and dismantle what is already attached, and must not strand a journey that
    is mid-flight at the bank.
    """
    settings = get_or_create_settings(session, current_user.uuid, master_key)
    if not settings.open_banking_enabled:
        raise HTTPException(
            status_code=403,
            detail="La connexion bancaire n'est pas activée dans vos paramètres.",
        )


@router.get("/status", response_model=BankConnectionStatus)
def get_connection_status(
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """Whether Enable Banking credentials are configured for this user."""
    return get_status(session, current_user.uuid, master_key)


@router.put(
    "/credentials",
    response_model=BankConnectionStatus,
    dependencies=[Depends(require_open_banking)],
)
def update_credentials(
    data: BankConnectionUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """Set or clear the Enable Banking application_id / private key."""
    return upsert_connection(session, current_user.uuid, master_key, data)


@router.get("/check", response_model=BankConfigCheck)
def check_config(
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """Pre-flight diagnostic (spec §C1): key valid, application active, callback declared."""
    settings = get_settings()
    return check_configuration(session, current_user.uuid, master_key, settings.banking_callback_url)


@router.get(
    "/aspsps",
    response_model=list[AspspSummary],
    dependencies=[Depends(require_open_banking)],
)
def get_aspsps(
    country: str,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """List available banks for a country."""
    try:
        return list_aspsps_for_country(session, current_user.uuid, master_key, country)
    except NotConfiguredError:
        raise HTTPException(status_code=400, detail="Configurez d'abord vos identifiants Enable Banking.")
    except BankingApiError as exc:
        raise HTTPException(status_code=502, detail=f"{exc.code}: {exc.message}")


@router.post(
    "/authorize",
    response_model=BankAuthorizeResponse,
    dependencies=[Depends(require_open_banking)],
)
def authorize(
    data: BankAuthorizeRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """Open the authorization journey (spec §C2): the browser must navigate to auth_url next."""
    settings = get_settings()
    try:
        auth_url = start_authorization_flow(
            session,
            current_user.uuid,
            master_key,
            data.aspsp_name,
            data.aspsp_country,
            settings.banking_callback_url,
        )
    except NotConfiguredError:
        raise HTTPException(status_code=400, detail="Configurez d'abord vos identifiants Enable Banking.")
    except AspspNotFoundError:
        raise HTTPException(status_code=404, detail="Banque inconnue pour ce pays.")
    except BankingApiError as exc:
        raise HTTPException(status_code=502, detail=f"{exc.code}: {exc.message}")
    return BankAuthorizeResponse(auth_url=auth_url)


_NO_SESSION_HTML = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<title>Connexion bancaire — CapitalView</title></head>
<body style="font-family: system-ui, sans-serif; max-width: 32rem; margin: 4rem auto; text-align: center;">
<h1>Autorisation bancaire reçue</h1>
<p>Votre banque vous a redirigé ici, mais cet onglet n'est pas connecté à CapitalView
(certaines applications bancaires mobiles ouvrent un nouveau navigateur).</p>
<p><strong>Retournez dans l'onglet où vous êtes connecté à CapitalView</strong> pour
terminer la liaison de votre compte.</p>
</body></html>"""


def _message_page(title: str, detail: str | None) -> str:
    # `detail` echoes the bank's `error` query parameter back: escaped, or this
    # page is a reflected-XSS sink on the very origin holding the Master Key cookie.
    safe_detail = html.escape(detail or "")
    return f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<title>{html.escape(title)} — CapitalView</title></head>
<body style="font-family: system-ui, sans-serif; max-width: 32rem; margin: 4rem auto; text-align: center;">
<h1>{html.escape(title)}</h1>
<p>{safe_detail}</p>
</body></html>"""


@router.get("/callback", include_in_schema=False)
def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    session: Session = Depends(get_session),
):
    """The bank's return (spec §C3). No Authorization header ever reaches this
    route — only cookies do. Authenticated via `state`, never a Bearer token."""
    master_key = request.cookies.get("master_key")
    if not master_key:
        return HTMLResponse(_NO_SESSION_HTML)

    try:
        decoded = base64.b64decode(master_key, validate=True)
        if len(decoded) != 32:
            raise ValueError("bad length")
    except Exception:
        return HTMLResponse(_NO_SESSION_HTML)

    result = handle_callback(session, master_key, code=code, state=state, error=error)

    if result.outcome == "success":
        # Nothing may raise past this point: the one-shot authorization code is
        # already spent, so a 500 here costs the user a fresh strong
        # authentication. An unset FRONTEND_URL degrades to a message page.
        frontend_url = get_settings().frontend_url
        if not frontend_url:
            return HTMLResponse(
                _message_page(
                    "Compte bancaire connecté",
                    "Votre banque est connectée. Retournez dans CapitalView pour "
                    "rattacher vos comptes.",
                )
            )
        return RedirectResponse(
            f"{frontend_url}/settings/banking?bank_session={result.bank_session_uuid}",
            status_code=302,
        )
    if result.outcome == "refused":
        return HTMLResponse(_message_page("Autorisation refusée", result.detail))
    if result.outcome == "invalid_state":
        return HTMLResponse(_message_page("Lien invalide", result.detail), status_code=400)
    return HTMLResponse(_message_page("Connexion impossible", result.detail))


@router.get("/sessions", response_model=list[BankSessionSummary])
def list_sessions(
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """The user's bank connections and the accounts attached to each."""
    return list_bank_sessions(session, current_user.uuid, master_key)


@router.get(
    "/sessions/{bank_session_uuid}/accounts",
    response_model=list[BankSessionAccount],
    dependencies=[Depends(require_open_banking)],
)
def get_session_accounts(
    bank_session_uuid: str,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """Accounts discovered in a bank session, and whether each is already linked (Step 6).

    Served from the payload captured at the callback — no Enable Banking call.
    """
    try:
        return list_session_accounts(session, current_user.uuid, master_key, bank_session_uuid)
    except BankSessionNotFoundError:
        raise HTTPException(status_code=404, detail="Session bancaire introuvable.")


@router.post(
    "/sessions/{bank_session_uuid}/link",
    response_model=BankAccountLinkResult,
    dependencies=[Depends(require_open_banking)],
)
def link_session_account(
    bank_session_uuid: str,
    data: BankAccountLinkRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """Rattachement (Step 6): attach a discovered account to a CapitalView bank
    account. Reconnections (matching identification_hash) update the existing
    link instead of creating a new one."""
    try:
        return link_account(
            session,
            current_user.uuid,
            master_key,
            bank_session_uuid,
            data.identification_hash,
            data.bank_account_uuid,
        )
    except BankSessionNotFoundError:
        raise HTTPException(status_code=404, detail="Session bancaire introuvable.")
    except TargetAccountNotFoundError:
        raise HTTPException(status_code=404, detail="Compte CapitalView introuvable.")
    except AccountNotFoundInSessionError:
        raise HTTPException(status_code=404, detail="Ce compte n'est pas dans la session bancaire.")


def _psu_context(request: Request) -> dict[str, str] | None:
    """PSU context headers, taken from the real request that triggered the sync.

    They describe the human behind the call, so they are read off that request
    and never fabricated. The API treats them as all-or-nothing (§B2), hence a
    partial context is sent as no context at all.
    """
    ip_address = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")
    if not ip_address or not user_agent:
        return None
    return {"Psu-Ip-Address": ip_address, "Psu-User-Agent": user_agent}


@router.post(
    "/sync",
    response_model=BankSyncResponse,
    dependencies=[Depends(require_open_banking)],
)
def sync(
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """Synchronise every linked account (spec §D, ruling R16).

    No body and no account identifier: the sync order is a server-side decision
    (ruling R12), not the caller's. The once-a-day cap is re-checked here —
    the front triggers this after every render, so a capped call is a 200 with
    an unchanged summary, never an error.
    """
    try:
        results = sync_user_accounts(
            session, current_user.uuid, master_key, psu_context=_psu_context(request)
        )
    except NotConfiguredError:
        raise HTTPException(status_code=400, detail="Configurez d'abord vos identifiants Enable Banking.")
    return BankSyncResponse(
        synced=sum(1 for result in results if result.status == "synced"),
        results=results,
    )


@router.delete("/sessions/{bank_session_uuid}", status_code=204)
def delete_session(
    bank_session_uuid: str,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """Disconnect a bank session: unlinks its accounts, closes the consent at
    Enable Banking (ruling R3: only ever exercised behind an injected double)."""
    try:
        delete_bank_session(session, current_user.uuid, master_key, bank_session_uuid)
    except BankSessionNotFoundError:
        raise HTTPException(status_code=404, detail="Session bancaire introuvable.")


@router.post(
    "/import-export",
    response_model=BankExportImportResponse,
    dependencies=[Depends(require_open_banking)],
)
def import_export(
    payload: dict[str, Any] | list[dict[str, Any]],
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """Import an Enable Banking JSON export file for history catch-up (Task 11)."""
    try:
        return import_enablebanking_export(session, current_user.uuid, master_key, payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

