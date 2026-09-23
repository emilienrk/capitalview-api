"""Enable Banking linking flow routes (spec §C).

GET /banking/callback is the one exception to the usual auth pattern: it's a
raw browser top-level GET navigation coming back from the bank, not an XHR
call from the SPA, so it never carries an Authorization header — only
whatever cookies ride along under SameSite=Lax. It authenticates itself via
`state` instead (see services/banking/linking.py).
"""

import base64
import html
from collections import defaultdict
from datetime import date
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Path, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlmodel import Session

from config import get_settings
from database import get_session
from dtos.auth import MessageResponse
from dtos.banking import (
    AspspSummary,
    BankAccountLinkRequest,
    BankAccountLinkResult,
    BankAccountUnlinkResult,
    BankAuthorizeRequest,
    BankAuthorizeResponse,
    BankConfigCheck,
    BankConnectionStatus,
    BankConnectionUpdate,
    BankExportImportResponse,
    BankFlowsResponse,
    BankLedger,
    BankReviewQueue,
    BankSessionAccount,
    BankSessionSummary,
    BankRecurringCreate,
    BankRecurringDecisionCreate,
    BankRecurringItem,
    BankRecurringMerge,
    BankRecurringOperation,
    BankRecurringResponse,
    BankRecurringUpdate,
    BankSyncResponse,
    BankTransactionItem,
    BankTransactionTypeResult,
    BankTransactionTypeUpdate,
    BankTransactionsResponse,
    BankTransferDecisionCreate,
    BankTransferQuestionMonth,
    BankTransferQuestionsResponse,
    BankTypeRuleItem,
    RealCashflowCurrent,
    RealCashflowMonthDetail,
    RealCashflowYear,
    RecurringDirection,
    SyncStatus,
)
from models import User
from services.auth import get_current_user, get_master_key
from services.banking.credentials import (
    get_status,
    upsert_connection,
)
from services.banking.export_import import import_enablebanking_export
from services.banking.flows import (
    LabelRequiredError,
    PairedOperationError,
    UnknownAccountError,
    clear_transaction_type,
    compute_real_flows,
    list_flow_group,
    list_month_transactions,
    list_transfer_counterparts,
    list_type_rules,
    review_queue,
    set_transaction_type,
    transfer_patterns,
)
from services.banking.transfer_decisions import (
    DecisionError,
    TransactionNotFoundError,
    record_decision,
)
from services.banking.errors import BankingApiError
from services.banking import recurring as recurring_service
from services.banking.recurring_decisions import RecurringNotFoundError
from services.banking.type_rules import RuleNotFoundError, delete_rule
from services.banking.ledger import build_ledger, ledger_etag
from services.banking.real_cashflow import (
    PeriodNotCompletedError,
    real_cashflow_current,
    real_cashflow_month,
    real_cashflow_year,
)
from services.banking.linking import (
    AccountNotFoundInSessionError,
    AspspNotFoundError,
    BankSessionNotFoundError,
    NotConfiguredError,
    TargetAccountNotFoundError,
    check_configuration,
    delete_bank_session,
    BankAccountNotLinkedError,
    CardAccountNotLinkableError,
    handle_callback,
    link_account,
    reseed_account_history,
    retry_account_sync,
    unlink_account,
    list_aspsps_for_country,
    TargetAccountAlreadyLinkedError,
    list_bank_sessions,
    list_session_accounts,
    start_authorization_flow,
)
from services.banking.sync import seed_after_linking, sync_user_accounts
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
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """Rattachement (Step 6): attach a discovered account to a CapitalView bank
    account. Reconnections (matching identification_hash) update the existing
    link instead of creating a new one.

    The first sync starts here, after the response, rather than waiting for the
    front to ask: some banks serve the full history only for minutes after the
    consent — see `seed_after_linking`. A reconnection gets it too, since a new
    consent is exactly what reopens that window."""
    try:
        result = link_account(
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
    except CardAccountNotLinkableError:
        raise HTTPException(
            status_code=409,
            detail="Un compte carte ne peut pas être rattaché : ses opérations sont déjà celles "
                   "du compte qu'il débite, et son solde n'est pas un solde de compte.",
        )
    except TargetAccountAlreadyLinkedError:
        raise HTTPException(
            status_code=409,
            detail="Ce compte CapitalView est déjà rattaché à un autre compte bancaire. "
                   "Un compte CapitalView ne peut en porter qu'un : chacun garde son propre "
                   "solde, sa propre ancre et sa propre courbe. Créez-en un second.",
        )
    background_tasks.add_task(
        seed_after_linking, current_user.uuid, master_key, _psu_context(request)
    )
    return result


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
        synced=sum(1 for result in results if result.status == SyncStatus.SYNCED),
        results=results,
    )


@router.post(
    "/accounts/{bank_account_uuid}/reseed-history",
    response_model=MessageResponse,
    dependencies=[Depends(require_open_banking)],
)
def reseed_account_history_route(
    bank_account_uuid: str,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """Ask the bank for this account's full history again on the next sync.

    Nothing is deleted: the seeding pass rewrites the window it can reach. The
    way out for an account whose first sync came back empty — until the flag
    existed, that left a flat curve with no way to retry short of detaching the
    account and authenticating at the bank all over again.
    """
    if reseed_account_history(session, current_user.uuid, master_key, bank_account_uuid) is None:
        raise HTTPException(
            status_code=404, detail="Ce compte n'est rattaché à aucun compte bancaire."
        )
    return MessageResponse(
        message="L'historique complet sera redemandé à votre banque à la prochaine synchronisation."
    )


@router.post(
    "/accounts/{bank_account_uuid}/retry-sync",
    response_model=MessageResponse,
    dependencies=[Depends(require_open_banking)],
)
def retry_account_sync_route(
    bank_account_uuid: str,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """Give a failed account its daily attempt back; the front syncs right after.

    The daily cap now counts failures too, so a sync that failed is not retried
    on its own before tomorrow. This is the user's way to try again sooner.
    """
    retried = retry_account_sync(session, current_user.uuid, master_key, bank_account_uuid)
    if retried is None:
        raise HTTPException(
            status_code=404, detail="Ce compte n'est rattaché à aucun compte bancaire."
        )
    if not retried:
        raise HTTPException(
            status_code=409, detail="La dernière synchronisation de ce compte n'a pas échoué."
        )
    return MessageResponse(message="La synchronisation va être relancée.")


@router.delete(
    "/accounts/{bank_account_uuid}/link",
    response_model=BankAccountUnlinkResult,
)
def unlink_bank_account(
    bank_account_uuid: str,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    delete_transactions: bool = False,
    session: Session = Depends(get_session),
):
    """Detach one account, leaving the authorization live for the others.

    Deliberately outside `require_open_banking`, like DELETE /sessions/{uuid}:
    turning the feature off must never trap the user with an attachment they
    can no longer undo.
    """
    try:
        return unlink_account(
            session, current_user.uuid, master_key, bank_account_uuid, delete_transactions
        )
    except BankAccountNotLinkedError:
        raise HTTPException(status_code=404, detail="Ce compte n'est rattaché à aucun compte bancaire.")


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


@router.get("/flows", response_model=BankFlowsResponse)
def get_flows(
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    months: int = 12,
    exclude_internal_transfers: bool = True,
    account_id: str | None = None,
    session: Session = Depends(get_session),
):
    """Observed inflow and outflow per month, from the stored transactions.

    Read-only and ungated like the other reads: it touches no credentials and
    reaches no bank, and someone who opted back out still owns this history.
    """
    try:
        return compute_real_flows(
            session,
            current_user.uuid,
            master_key,
            months=months,
            exclude_internal_transfers=exclude_internal_transfers,
            account_id=account_id,
        )
    except UnknownAccountError:
        raise HTTPException(status_code=404, detail="Compte bancaire introuvable.")


@router.get("/transactions", response_model=BankTransactionsResponse)
def get_transactions(
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    period: Annotated[str | None, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")] = None,
    account_id: str | None = None,
    session: Session = Depends(get_session),
):
    """One month of stored operations, newest first. Ungated, like /flows.

    A month rather than a page: dates are stored encrypted, and the month's
    blind index is the only date a query can filter on.
    """
    try:
        return list_month_transactions(
            session,
            current_user.uuid,
            master_key,
            period=period or date.today().strftime("%Y-%m"),
            account_id=account_id,
        )
    except UnknownAccountError:
        raise HTTPException(status_code=404, detail="Compte bancaire introuvable.")


@router.get("/transactions/{transaction_id}/counterparts", response_model=list[BankTransactionItem])
def get_transfer_counterparts(
    transaction_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """The operations that could be bound to this one, as a transfer or as its
    cancellation. Ungated, like /transactions."""
    try:
        return list_transfer_counterparts(session, current_user.uuid, master_key, transaction_id)
    except TransactionNotFoundError:
        raise HTTPException(status_code=404, detail="Opération introuvable.")


@router.get("/transactions/{transaction_id}/flow-group", response_model=list[BankTransactionItem])
def get_flow_group(
    transaction_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """The operations one answer to this flow question would type, newest first.
    Ungated, like /transactions."""
    try:
        return list_flow_group(session, current_user.uuid, master_key, transaction_id)
    except TransactionNotFoundError:
        raise HTTPException(status_code=404, detail="Opération introuvable.")


@router.put("/transactions/{transaction_id}/type", response_model=BankTransactionTypeResult)
def put_transaction_type(
    transaction_id: str,
    body: BankTransactionTypeUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """Type this operation, or every operation reading like it on its account
    and direction. Ungated, like /transactions."""
    try:
        return set_transaction_type(
            session, current_user.uuid, master_key, transaction_id, body.type, body.scope,
        )
    except TransactionNotFoundError:
        raise HTTPException(status_code=404, detail="Opération introuvable.")
    except PairedOperationError:
        raise HTTPException(
            status_code=409,
            detail="Cette opération est appariée à une autre : défaites d'abord le virement ou l'annulation.",
        )
    except LabelRequiredError:
        raise HTTPException(status_code=400, detail="Une opération sans libellé ne se corrige qu'à l'unité.")


@router.delete("/transactions/{transaction_id}/type", response_model=BankTransactionItem)
def delete_transaction_type(
    transaction_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """Drop the type forced on this one operation."""
    try:
        return clear_transaction_type(session, current_user.uuid, master_key, transaction_id)
    except TransactionNotFoundError:
        raise HTTPException(status_code=404, detail="Opération introuvable.")


@router.get("/type-rules", response_model=list[BankTypeRuleItem])
def get_type_rules(
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    return list_type_rules(session, current_user.uuid, master_key)


@router.delete("/type-rules/{rule_id}", status_code=204)
def delete_type_rule(
    rule_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    try:
        delete_rule(session, current_user.uuid, master_key, rule_id)
    except RuleNotFoundError:
        raise HTTPException(status_code=404, detail="Règle introuvable.")


@router.get("/transfer-questions", response_model=BankTransferQuestionsResponse)
def get_transfer_questions(
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """How many pairs, flow and recurring payment questions wait for the user, and
    in which months. Ungated, like /transactions."""
    patterns = transfer_patterns(session, current_user.uuid, master_key)
    questions = defaultdict(int, patterns.questions)
    for period, count in [*patterns.flow_questions.items(), *patterns.recurring_questions.items()]:
        questions[period] += count
    questions = dict(sorted(questions.items()))
    return BankTransferQuestionsResponse(
        total=sum(questions.values()),
        months=[BankTransferQuestionMonth(period=p, count=n) for p, n in questions.items()],
    )


@router.get("/review-queue", response_model=BankReviewQueue)
def get_review_queue(
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    year: Annotated[int | None, Query(ge=1970, le=9999)] = None,
    session: Session = Depends(get_session),
):
    """Every open question, the heaviest first. Ungated, like /transactions."""
    return review_queue(session, current_user.uuid, master_key, year)


@router.get("/ledger", response_model=BankLedger)
def get_ledger(
    request: Request,
    response: Response,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """Every stored operation, typed. Ungated, like /transactions.

    Answered 304 when nothing it is read from changed: the whole history is
    the heaviest read there is, and the Explorer asks for it on every visit.
    """
    etag = f'"{ledger_etag(session, current_user.uuid, master_key)}"'
    headers = {"ETag": etag, "Cache-Control": "private, no-cache"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    response.headers.update(headers)
    return build_ledger(session, current_user.uuid, master_key)


@router.post("/transfer-decisions", status_code=204)
def post_transfer_decision(
    body: BankTransferDecisionCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """Settle two operations: one internal transfer, not one, or an operation
    and its cancellation. Replaces what was decided about them before."""
    try:
        record_decision(
            session, current_user.uuid, master_key,
            body.transaction_id, body.other_transaction_id, body.kind,
        )
    except TransactionNotFoundError:
        raise HTTPException(status_code=404, detail="Opération introuvable.")
    except DecisionError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


_NO_OPERATION = "Opération introuvable."
_NO_RECURRING = "Récurrent introuvable."
_NO_SERIES = "Cette opération n'appartient à aucun paiement ni revenu récurrent."


@router.get("/recurring", response_model=BankRecurringResponse)
def get_recurring(
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
    direction: RecurringDirection = RecurringDirection.EXPENSE,
):
    """Every recurring payment — or, with `direction=income`, every recurring
    income — found or decided, from the stored operations. Ungated, like
    /transactions."""
    return recurring_service.list_recurring(session, current_user.uuid, master_key, direction=direction)


@router.get("/recurring/operations", response_model=list[BankTransactionItem])
def get_undecided_recurring_operations(
    transaction_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """The operations of the recurring payment this operation belongs to, decided or not."""
    try:
        return recurring_service.recurring_operations(
            session, current_user.uuid, master_key, transaction_id=transaction_id,
        )
    except TransactionNotFoundError:
        raise HTTPException(status_code=404, detail=_NO_OPERATION)
    except recurring_service.NoRecurringError:
        raise HTTPException(status_code=404, detail=_NO_SERIES)


@router.get("/recurring/{recurring_id}/operations", response_model=list[BankTransactionItem])
def get_recurring_operations(
    recurring_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    try:
        return recurring_service.recurring_operations(
            session, current_user.uuid, master_key, recurring_id=recurring_id,
        )
    except RecurringNotFoundError:
        raise HTTPException(status_code=404, detail=_NO_RECURRING)


@router.post("/recurring/decisions", response_model=BankRecurringItem | None)
def post_recurring_decision(
    body: BankRecurringDecisionCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """Yes or no to the recurring payment an operation belongs to."""
    try:
        return recurring_service.decide(
            session, current_user.uuid, master_key, body.transaction_id, body.decision, body.name,
        )
    except TransactionNotFoundError:
        raise HTTPException(status_code=404, detail=_NO_OPERATION)
    except recurring_service.NoRecurringError:
        raise HTTPException(status_code=404, detail=_NO_SERIES)


@router.post("/recurring", response_model=BankRecurringItem | None, status_code=201)
def post_recurring(
    body: BankRecurringCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """Mark an operation the detection missed as a recurring payment, or as a
    recurring income for a credit."""
    try:
        return recurring_service.mark(
            session, current_user.uuid, master_key, body.transaction_id, body.cadence, body.name,
        )
    except TransactionNotFoundError:
        raise HTTPException(status_code=404, detail=_NO_OPERATION)
    except recurring_service.NotMarkableError:
        raise HTTPException(
            status_code=409,
            detail="Seule une opération passée, hors virement entre vos comptes, peut être marquée comme "
                   "récurrente : un débit compté en dépense ou un crédit compté en revenu.",
        )


@router.patch("/recurring/{recurring_id}", response_model=BankRecurringItem | None)
def patch_recurring(
    recurring_id: str,
    body: BankRecurringUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """Rename it, force its cadence, say what it is for or when it was ended."""
    try:
        return recurring_service.update(
            session, current_user.uuid, master_key, recurring_id,
            {name: getattr(body, name) for name in body.model_fields_set},
        )
    except RecurringNotFoundError:
        raise HTTPException(status_code=404, detail=_NO_RECURRING)
    except recurring_service.NatureMismatchError:
        raise HTTPException(
            status_code=422, detail="Cette nature ne correspond pas au sens de ce récurrent (dépense ou revenu).",
        )


@router.post("/recurring/{recurring_id}/operations", response_model=BankRecurringItem | None)
def post_recurring_operation(
    recurring_id: str,
    body: BankRecurringOperation,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """Attach an operation to it, or detach one from it."""
    try:
        return recurring_service.correct(
            session, current_user.uuid, master_key, recurring_id, body.transaction_id, body.action,
        )
    except TransactionNotFoundError:
        raise HTTPException(status_code=404, detail=_NO_OPERATION)
    except RecurringNotFoundError:
        raise HTTPException(status_code=404, detail=_NO_RECURRING)


@router.post("/recurring/{recurring_id}/merge", response_model=BankRecurringItem | None)
def post_recurring_merge(
    recurring_id: str,
    body: BankRecurringMerge,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """Make one recurring payment of two."""
    if (body.other_id is None) == (body.other_transaction_id is None):
        raise HTTPException(status_code=400, detail="Indiquez l'autre paiement récurrent, par son id ou par une opération.")
    try:
        return recurring_service.merge(
            session, current_user.uuid, master_key, recurring_id, body.other_id, body.other_transaction_id,
        )
    except TransactionNotFoundError:
        raise HTTPException(status_code=404, detail=_NO_OPERATION)
    except RecurringNotFoundError:
        raise HTTPException(status_code=404, detail=_NO_RECURRING)
    except recurring_service.NoRecurringError:
        raise HTTPException(status_code=404, detail=_NO_SERIES)
    except recurring_service.DirectionMismatchError:
        raise HTTPException(status_code=409, detail="Un paiement et un revenu ne se fusionnent pas.")


@router.delete("/recurring/{recurring_id}", status_code=204)
def delete_recurring(
    recurring_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """Forget the decision: the series is found and asked about again."""
    try:
        recurring_service.forget(session, current_user.uuid, master_key, recurring_id)
    except RecurringNotFoundError:
        raise HTTPException(status_code=404, detail=_NO_RECURRING)


@router.get("/real-cashflow", response_model=RealCashflowYear)
def get_real_cashflow(
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    year: Annotated[int | None, Query(ge=1970, le=9999)] = None,
    session: Session = Depends(get_session),
):
    """What was earned, spent, set aside and invested over a year's completed
    months, from the stored operations. Ungated, like /flows."""
    return real_cashflow_year(session, current_user.uuid, master_key, year)


@router.get("/real-cashflow/current", response_model=RealCashflowCurrent)
def get_real_cashflow_current(
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """The month in progress, day by day, against the recent months."""
    return real_cashflow_current(session, current_user.uuid, master_key)


@router.get("/real-cashflow/months/{period}", response_model=RealCashflowMonthDetail)
def get_real_cashflow_month(
    period: Annotated[str, Path(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")],
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    try:
        return real_cashflow_month(session, current_user.uuid, master_key, period)
    except PeriodNotCompletedError:
        raise HTTPException(status_code=400, detail="Seul un mois terminé a un cashflow réel.")


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

