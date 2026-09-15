"""Enable Banking linking flow routes (spec §C).

GET /banking/callback is the one exception to the usual auth pattern: it's a
raw browser top-level GET navigation coming back from the bank, not an XHR
call from the SPA, so it never carries an Authorization header — only
whatever cookies ride along under SameSite=Lax. It authenticates itself via
`state` instead (see services/banking/linking.py).
"""

import base64
import html
from datetime import date
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session

from config import get_settings
from database import get_session
from dtos.auth import MessageResponse
from dtos.banking import (
    AspspSummary,
    AvailableCategory,
    BankCategoryAssign,
    BankCategoryAssignResult,
    BankCategoryCreate,
    BankCategoryItem,
    BankCategoryRuleItem,
    BankCategoryUpdate,
    BankRuleWords,
    BankAICategorizeResult,
    BankUncategorizedResponse,
    CategoryOrigin,
    CategoryScope,
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
    BankSessionAccount,
    BankSessionSummary,
    BankSyncResponse,
    BankTransactionItem,
    BankTransactionsResponse,
    BankTransferDecisionCreate,
    BankTransferQuestionMonth,
    BankTransferQuestionsResponse,
    SyncStatus,
)
from models import User
from services.ai.agents import categorize_agent
from services.ai.manager import NoProviderAvailableError
from services.auth import get_current_user, get_master_key
from services.banking.credentials import (
    get_status,
    upsert_connection,
)
from services.banking.export_import import import_enablebanking_export
from services.banking.categories import (
    CategoryNameTakenError,
    CategoryNotFoundError,
    InvalidCategoryNameError,
    RuleNotFoundError,
    available_categories,
    create_category,
    delete_category,
    delete_rule,
    list_categories,
    list_rules,
    materialize_cashflow_category,
    rename_category,
    set_category_nature,
)
from services.banking.categorize import EmptyRuleError, TooGeneralRuleError
from services.banking.flows import (
    CategoryRequiredError,
    RuleOutsideLabelError,
    UnknownAccountError,
    assign_category,
    compute_real_flows,
    list_month_transactions,
    list_transfer_counterparts,
    rule_words,
    transfer_patterns,
    uncategorized_groups,
)
from services.banking.transfer_decisions import (
    DecisionError,
    TransactionNotFoundError,
    record_decision,
)
from services.banking.errors import BankingApiError
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


@router.get("/transfer-questions", response_model=BankTransferQuestionsResponse)
def get_transfer_questions(
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """How many pairs wait for the user, and in which months. Ungated, like /transactions."""
    questions = transfer_patterns(session, current_user.uuid, master_key).questions
    return BankTransferQuestionsResponse(
        total=sum(questions.values()),
        months=[BankTransferQuestionMonth(period=p, count=n) for p, n in questions.items()],
    )


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


_CATEGORY_NOT_FOUND = "Catégorie introuvable."


def _category_or_error(action):
    try:
        return action()
    except CategoryNotFoundError:
        raise HTTPException(status_code=404, detail=_CATEGORY_NOT_FOUND)
    except CategoryNameTakenError:
        raise HTTPException(status_code=409, detail="Une catégorie porte déjà ce nom.")
    except InvalidCategoryNameError:
        raise HTTPException(status_code=400, detail="Le nom d'une catégorie compte de 1 à 60 caractères.")


@router.get("/categories", response_model=list[BankCategoryItem])
def get_categories(
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """Every category of the user, with how many rules file into each."""
    return list_categories(session, current_user.uuid, master_key)


@router.get("/categories/available", response_model=list[AvailableCategory])
def get_available_categories(
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    scope: CategoryScope = CategoryScope.BANK,
    session: Session = Depends(get_session),
):
    """The categories a screen offers, which depends on AI categorisation."""
    settings = get_or_create_settings(session, current_user.uuid, master_key)
    return available_categories(
        session, current_user.uuid, master_key, scope, settings.ai_categorization_enabled,
    )


@router.post("/categories", response_model=BankCategoryItem, status_code=201)
def post_category(
    body: BankCategoryCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """Create a category in Banque, or materialise a declared cashflow's."""
    def create():
        if body.from_cashflow:
            return materialize_cashflow_category(session, current_user.uuid, master_key, body.name)
        return create_category(
            session, current_user.uuid, master_key, body.name, body.nature, CategoryOrigin.BANK,
        )

    category = _category_or_error(create)
    return BankCategoryItem(id=category.uuid, name=category.name, nature=category.nature, origin=category.origin)


@router.patch("/categories/{category_id}", response_model=BankCategoryItem)
def patch_category(
    category_id: str,
    body: BankCategoryUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    def update():
        if body.name is not None:
            rename_category(session, current_user.uuid, master_key, category_id, body.name)
        if body.nature is not None:
            set_category_nature(session, current_user.uuid, master_key, category_id, body.nature)

    _category_or_error(update)
    category = next((c for c in list_categories(session, current_user.uuid, master_key) if c.id == category_id), None)
    if category is None:
        raise HTTPException(status_code=404, detail=_CATEGORY_NOT_FOUND)
    return category


@router.delete("/categories/{category_id}", status_code=204)
def delete_category_route(
    category_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """Delete a category and its rules; operations filed by hand under it read as uncategorised."""
    _category_or_error(lambda: delete_category(session, current_user.uuid, master_key, category_id))


@router.get("/transactions/{transaction_id}/rule-tokens", response_model=BankRuleWords)
def get_rule_tokens(
    transaction_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """The words a rule for this operation could require, and those proposed."""
    try:
        return rule_words(session, current_user.uuid, master_key, transaction_id)
    except TransactionNotFoundError:
        raise HTTPException(status_code=404, detail="Opération introuvable.")


@router.put("/transactions/{transaction_id}/category", response_model=BankCategoryAssignResult)
def put_transaction_category(
    transaction_id: str,
    body: BankCategoryAssign,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    """File one operation, or every operation like it through a rule."""
    try:
        return assign_category(
            session, current_user.uuid, master_key, transaction_id,
            body.category_id, body.apply_to_similar, body.tokens,
        )
    except TransactionNotFoundError:
        raise HTTPException(status_code=404, detail="Opération introuvable.")
    except CategoryNotFoundError:
        raise HTTPException(status_code=404, detail=_CATEGORY_NOT_FOUND)
    except CategoryRequiredError:
        raise HTTPException(status_code=400, detail="Choisissez une catégorie à appliquer aux opérations similaires.")
    except EmptyRuleError:
        raise HTTPException(status_code=400, detail="Gardez au moins un mot du libellé.")
    except TooGeneralRuleError:
        raise HTTPException(
            status_code=400,
            detail="Ces mots se retrouvent dans trop d'opérations : gardez-en un plus distinctif.",
        )
    except RuleOutsideLabelError:
        raise HTTPException(status_code=400, detail="Les mots d'une règle doivent venir du libellé de l'opération.")


@router.get("/category-rules", response_model=list[BankCategoryRuleItem])
def get_category_rules(
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    return list_rules(session, current_user.uuid, master_key)


@router.delete("/category-rules/{rule_id}", status_code=204)
def delete_category_rule(
    rule_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    session: Session = Depends(get_session),
):
    try:
        delete_rule(session, current_user.uuid, master_key, rule_id)
    except RuleNotFoundError:
        raise HTTPException(status_code=404, detail="Règle introuvable.")


@router.get("/uncategorized", response_model=BankUncategorizedResponse)
def get_uncategorized(
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    session: Session = Depends(get_session),
):
    """The operations left to file, grouped, heaviest first. Ungated, like /transactions."""
    return uncategorized_groups(session, current_user.uuid, master_key, limit)


@router.post("/categorize/ai", response_model=BankAICategorizeResult)
async def post_ai_categorization(
    current_user: Annotated[User, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
    skip: Annotated[int, Query(ge=0)] = 0,
    session: Session = Depends(get_session),
):
    """File the next batch of the heaviest groups left to file with the AI.

    The front calls again, passing back `skip`, while `remaining` is positive:
    no background job, so the Master Key never outlives a request.
    """
    settings = get_or_create_settings(session, current_user.uuid, master_key)
    if not (settings.ai_feature_enabled and settings.ai_categorization_enabled):
        raise HTTPException(
            status_code=403, detail="La catégorisation par IA n'est pas activée dans vos paramètres.",
        )
    try:
        agent = categorize_agent.build_categorize_agent(session, current_user.uuid, master_key)
    except NoProviderAvailableError:
        raise HTTPException(status_code=400, detail="Configurez d'abord un fournisseur d'IA.")
    return await categorize_agent.run_ai_categorization(session, current_user.uuid, master_key, agent, skip)


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

