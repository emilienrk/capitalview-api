"""
Enable Banking linking flow: configuration check, authorization, callback, rattachement.

Separate from client.py (raw API access) and credentials.py (BYO key storage).
This module orchestrates the two into the user-facing journey (spec §C):
pre-flight config check, opening a bank authorization, handling its return,
and attaching discovered accounts to CapitalView bank accounts.

The callback route is the one caller here that never has a Bearer-authenticated
user: it authenticates the browser purely via `state` + the Master Key cookie
(spec §C3), so its entry point (handle_callback) takes user_uuid_bidx from the
BankAuthorization row rather than a raw user_uuid.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal

from sqlmodel import Session, select

from dtos.banking import (
    AspspSummary,
    BankAccountLinkResult,
    BankConfigCheck,
    BankSessionAccount,
    BankSessionLinkedAccount,
    BankSessionSummary,
)
from models.bank import BankAccount
from models.banking import BankAccountLink, BankAuthorization, BankSession
from services.banking.client import build_client
from services.banking.credentials import (
    get_decrypted_credentials,
    get_decrypted_credentials_by_bidx,
)
from services.banking.errors import AuthorizationInvalidError, BankingApiError
from services.banking.health import is_session_active, session_status_message
from services.encryption import decrypt_data, encrypt_data, hash_index

# Window to complete the bank's own authentication before our side of the
# flow is considered abandoned. Independent of the bank's session valid_until.
AUTHORIZATION_TTL = timedelta(hours=1)

# SessionStatus members, referenced by NAME. The contract's x-enum-descriptions
# are misaligned with their values (documented trap), so the position of a value
# in that enum means nothing — never index into it.
STATUS_AUTHORIZED = "AUTHORIZED"
STATUS_CLOSED = "CLOSED"

# CashAccountType member, also by NAME: an account used for card payments only.
CARD_ACCOUNT_TYPE = "CARD"


class LinkingError(Exception):
    """Base for user-facing linking-flow errors; routes map these to HTTP responses."""


class NotConfiguredError(LinkingError):
    """No Enable Banking application_id/private_key configured for this user."""


class AspspNotFoundError(LinkingError):
    """The requested bank isn't in the catalogue for that country (spec §B5: reload it, it may have renamed)."""


class BankSessionNotFoundError(LinkingError):
    """No bank session with that uuid belongs to this user."""


class TargetAccountNotFoundError(LinkingError):
    """The CapitalView bank account to attach to doesn't belong to this user."""


class AccountNotFoundInSessionError(LinkingError):
    """The identification_hash isn't among the session's current accounts."""


# ---------------------------------------------------------------------------
# C1 — Configuration check
# ---------------------------------------------------------------------------


def check_configuration(
    session: Session, user_uuid: str, master_key: str, callback_url: str
) -> BankConfigCheck:
    """GET /application in one call: key validity, application active, callback declared."""
    creds = get_decrypted_credentials(session, user_uuid, master_key)
    if creds is None:
        return BankConfigCheck(
            configured=False,
            key_valid=False,
            application_active=False,
            callback_url_declared=False,
            callback_url=callback_url,
            error="Aucun identifiant Enable Banking configuré.",
        )

    try:
        # A malformed private key fails at JWT-signing time, inside build_client
        # itself, before any HTTP call is made — never a BankingApiError. This
        # diagnostic's whole job is to never crash, so both are caught here.
        with build_client(*creds) as client:
            data = client.get_application()
    except BankingApiError as exc:
        return BankConfigCheck(
            configured=True,
            key_valid=False,
            application_active=False,
            callback_url_declared=False,
            callback_url=callback_url,
            error=f"{exc.code}: {exc.message}",
        )
    except Exception as exc:
        return BankConfigCheck(
            configured=True,
            key_valid=False,
            application_active=False,
            callback_url_declared=False,
            callback_url=callback_url,
            error=f"Clé Enable Banking invalide : {exc}",
        )

    redirect_urls = data.get("redirect_urls", [])
    return BankConfigCheck(
        configured=True,
        key_valid=True,
        application_active=bool(data.get("active")),
        callback_url_declared=callback_url in redirect_urls,
        callback_url=callback_url,
        error=None,
    )


# ---------------------------------------------------------------------------
# ASPSP catalogue
# ---------------------------------------------------------------------------


def list_aspsps_for_country(
    session: Session, user_uuid: str, master_key: str, country: str
) -> list[AspspSummary]:
    creds = get_decrypted_credentials(session, user_uuid, master_key)
    if creds is None:
        raise NotConfiguredError()

    with build_client(*creds) as client:
        data = client.list_aspsps(country)

    return [
        AspspSummary(
            name=aspsp["name"],
            country=aspsp["country"],
            logo=aspsp.get("logo"),
            beta=bool(aspsp.get("beta", False)),
            maximum_consent_validity=aspsp["maximum_consent_validity"],
        )
        for aspsp in data.get("aspsps", [])
    ]


# ---------------------------------------------------------------------------
# C2 — Opening the authorization
# ---------------------------------------------------------------------------


def start_authorization_flow(
    session: Session,
    user_uuid: str,
    master_key: str,
    aspsp_name: str,
    aspsp_country: str,
    callback_url: str,
) -> str:
    """POST /auth: random state persisted as a blind index, valid_until requested
    at the bank's own maximum (spec §C2 — asking for less imposes re-authentication
    far more often than the bank would actually require)."""
    creds = get_decrypted_credentials(session, user_uuid, master_key)
    if creds is None:
        raise NotConfiguredError()

    with build_client(*creds) as client:
        catalogue = client.list_aspsps(aspsp_country)
        aspsp = next(
            (a for a in catalogue.get("aspsps", []) if a["name"] == aspsp_name), None
        )
        if aspsp is None:
            raise AspspNotFoundError(aspsp_name, aspsp_country)

        max_seconds = aspsp["maximum_consent_validity"]
        valid_until = (datetime.now(timezone.utc) + timedelta(seconds=max_seconds)).isoformat()

        state = secrets.token_urlsafe(32)
        authorization = BankAuthorization(
            user_uuid_bidx=hash_index(user_uuid, master_key),
            state_bidx=hash_index(state, master_key),
            aspsp_name_enc=encrypt_data(aspsp_name, master_key),
            aspsp_country_enc=encrypt_data(aspsp_country, master_key),
            expires_at=datetime.now(timezone.utc) + AUTHORIZATION_TTL,
        )
        session.add(authorization)
        session.commit()

        response = client.start_authorization(
            aspsp_name=aspsp_name,
            aspsp_country=aspsp_country,
            redirect_url=callback_url,
            state=state,
            valid_until=valid_until,
            psu_type="personal",
        )

        authorization.authorization_id_enc = encrypt_data(response["authorization_id"], master_key)
        session.add(authorization)
        session.commit()

        return response["url"]


# ---------------------------------------------------------------------------
# C3 — The return
# ---------------------------------------------------------------------------


@dataclass
class CallbackResult:
    outcome: Literal["success", "refused", "invalid_state", "error"]
    bank_session_uuid: str | None = None
    detail: str | None = None


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def handle_callback(
    session: Session,
    master_key: str,
    code: str | None,
    state: str | None,
    error: str | None,
) -> CallbackResult:
    """Three outcomes, not two (spec §C3): success (code+state), refusal
    (error=access_denied), or a technical failure. `state` is the only proof
    this browser is the one that opened the flow — the API never mentions
    this requirement, it's ours."""
    if error is not None:
        if state:
            _expire_authorization(session, master_key, state)
        if error == "access_denied":
            return CallbackResult(
                outcome="refused",
                detail="Vous avez refusé ou annulé l'autorisation d'accès à votre compte bancaire.",
            )
        return CallbackResult(outcome="error", detail=f"La banque a retourné une erreur : {error}")

    if not code or not state:
        return CallbackResult(outcome="error", detail="Réponse de callback incomplète.")

    now = datetime.now(timezone.utc)
    state_bidx = hash_index(state, master_key)
    # Expiry filtered in SQL, not compared in Python after fetch (matches
    # RefreshToken's pattern in services/auth.py): SQLite can hand back a
    # naive datetime on read, which would blow up a naive/aware comparison.
    authorization = session.exec(
        select(BankAuthorization).where(
            BankAuthorization.state_bidx == state_bidx,
            BankAuthorization.expires_at > now,
        )
    ).first()
    if authorization is None:
        return CallbackResult(
            outcome="invalid_state", detail="Autorisation inconnue, déjà utilisée ou expirée."
        )

    creds = get_decrypted_credentials_by_bidx(session, authorization.user_uuid_bidx, master_key)
    if creds is None:
        return CallbackResult(
            outcome="error", detail="Configuration Enable Banking introuvable pour cet utilisateur."
        )

    with build_client(*creds) as client:
        try:
            response = client.create_session(code)
        except AuthorizationInvalidError:
            # Replayed/expired code (spec §B5): "idempotence covers the replay" —
            # never surface as a crash, just ask the user to restart the journey.
            # The BankAuthorization row is left in place; it ages out via expires_at.
            return CallbackResult(
                outcome="error",
                detail="Ce lien d'autorisation a déjà été utilisé. Merci de relancer la connexion bancaire.",
            )
        except BankingApiError as exc:
            return CallbackResult(outcome="error", detail=f"{exc.code}: {exc.message}")

    accounts = response.get("accounts", [])
    bank_session = BankSession(
        user_uuid_bidx=authorization.user_uuid_bidx,
        session_id_enc=encrypt_data(response["session_id"], master_key),
        aspsp_name_enc=encrypt_data(response["aspsp"]["name"], master_key),
        aspsp_country_enc=encrypt_data(response["aspsp"]["country"], master_key),
        # A successful POST /sessions response has no other possible status.
        status=STATUS_AUTHORIZED,
        consent_valid_until=_parse_datetime(response["access"]["valid_until"]),
        authorized_at=now,
        # §C4: the accounts payload is delivered exactly once. GET /sessions/{id}
        # later returns SessionAccount (uid + identification hashes only), so
        # name, IBAN, currency, product and the rest exist nowhere else. Stored
        # verbatim rather than trimmed: what a later task needs isn't knowable now.
        accounts_enc=encrypt_data(json.dumps(accounts), master_key),
    )
    session.add(bank_session)

    # Reconnections: an account already linked (by identification_hash, the
    # durable key — uid is disposable) is repointed at the new session in
    # place. New accounts are presented, not linked yet (ruling R5): a link
    # needs a CapitalView bank_accounts.uuid, chosen at the rattachement step.
    for account in accounts:
        identification_hash = account.get("identification_hash")
        uid = account.get("uid")
        if not identification_hash or not uid:
            continue
        existing_link = _find_link_by_ident(
            session, authorization.user_uuid_bidx, hash_index(identification_hash, master_key)
        )
        if existing_link is not None:
            existing_link.session_uuid = bank_session.uuid
            existing_link.account_uid_enc = encrypt_data(uid, master_key)
            session.add(existing_link)

    _retire_superseded_sessions(session, master_key, authorization.user_uuid_bidx, bank_session)

    session.delete(authorization)
    session.commit()
    session.refresh(bank_session)

    return CallbackResult(outcome="success", bank_session_uuid=bank_session.uuid)


def _retire_superseded_sessions(
    session: Session, master_key: str, user_uuid_bidx: str, new_session: BankSession
) -> None:
    """Mark previous consents to the same bank as CLOSED once nothing points at them.

    Every reconnection inserts a new row; without this the old one keeps
    status=AUTHORIZED and its stale consent_valid_until forever, and the
    Master-Key-less expiry job (spec §A3) would notify on a dead consent. Only
    sessions no link references are retired — a link still pointing at one means
    that account wasn't re-discovered, and its consent is genuinely still live.
    Retiring means updating status: bank_account_links.session_uuid is RESTRICT.
    """
    aspsp = _decrypt_aspsp(new_session, master_key)
    if aspsp is None:
        return

    # The repointing loop above only touched the ORM; flush so the link/session
    # queries below see the new session_uuid values.
    session.flush()

    candidates = session.exec(
        select(BankSession).where(
            BankSession.user_uuid_bidx == user_uuid_bidx,
            BankSession.status == STATUS_AUTHORIZED,
            BankSession.uuid != new_session.uuid,
        )
    ).all()
    for candidate in candidates:
        if _decrypt_aspsp(candidate, master_key) != aspsp:
            continue
        still_linked = session.exec(
            select(BankAccountLink).where(BankAccountLink.session_uuid == candidate.uuid)
        ).first()
        if still_linked is None:
            candidate.status = STATUS_CLOSED
            session.add(candidate)


def _decrypt_aspsp(bank_session: BankSession, master_key: str) -> tuple[str, str] | None:
    """The bank a session belongs to, or None when it wasn't recorded."""
    if bank_session.aspsp_name_enc is None or bank_session.aspsp_country_enc is None:
        return None
    return (
        decrypt_data(bank_session.aspsp_name_enc, master_key),
        decrypt_data(bank_session.aspsp_country_enc, master_key),
    )


def _expire_authorization(session: Session, master_key: str, state: str) -> None:
    """Best-effort cleanup on refusal/error: remove the now-dead authorization row."""
    state_bidx = hash_index(state, master_key)
    authorization = session.exec(
        select(BankAuthorization).where(BankAuthorization.state_bidx == state_bidx)
    ).first()
    if authorization is not None:
        session.delete(authorization)
        session.commit()


# ---------------------------------------------------------------------------
# Rattachement (Step 6, ruling R5): BankAccountLink rows are created here, not
# in the callback, because bank_account_uuid_bidx is unique and points at a
# CapitalView account that must already exist.
# ---------------------------------------------------------------------------


def _accounts_bank_account_uuid_by_bidx(
    session: Session, user_bidx: str, master_key: str
) -> dict[str, str]:
    """bank_account_uuid_bidx is one-way (search-only), so recovering which raw
    CapitalView account a link points at means recomputing the hash for each of
    the user's own accounts and matching — there's no way to invert it."""
    accounts = session.exec(
        select(BankAccount).where(BankAccount.user_uuid_bidx == user_bidx)
    ).all()
    return {hash_index(account.uuid, master_key): account.uuid for account in accounts}


def _load_owned_session(
    session: Session, user_uuid: str, master_key: str, bank_session_uuid: str
) -> tuple[BankSession, str]:
    """A bank session that provably belongs to this user, plus their blind index.

    Single home for the ownership predicate: a future tightening of it must not
    be able to land on two call sites out of three.
    """
    user_bidx = hash_index(user_uuid, master_key)
    bank_session = session.get(BankSession, bank_session_uuid)
    if bank_session is None or bank_session.user_uuid_bidx != user_bidx:
        raise BankSessionNotFoundError()
    return bank_session, user_bidx


def _find_link_by_ident(
    session: Session, user_uuid_bidx: str, identification_hash_bidx: str
) -> BankAccountLink | None:
    """The user's link for one discovered account, found on the durable key."""
    return session.exec(
        select(BankAccountLink).where(
            BankAccountLink.identification_hash_bidx == identification_hash_bidx,
            BankAccountLink.user_uuid_bidx == user_uuid_bidx,
        )
    ).first()


def _stored_accounts(bank_session: BankSession, master_key: str) -> list[dict[str, Any]]:
    """The POST /sessions accounts payload captured at the callback (§C4)."""
    if not bank_session.accounts_enc:
        return []
    return json.loads(decrypt_data(bank_session.accounts_enc, master_key))


def find_discovered_account(
    session: Session, link: BankAccountLink, master_key: str
) -> dict[str, Any]:
    """The discovered-account payload a link points at, or {} when it is gone.

    identification_hash_bidx is one-way, so the match is made by re-hashing each
    of the session's own accounts — the same inversion `_accounts_bank_account_
    uuid_by_bidx` performs. Shared with the sync (R12's ordering and R19's
    "not reconcilable" both read `cash_account_type` from here).
    """
    bank_session = session.get(BankSession, link.session_uuid)
    if bank_session is None:
        return {}
    for account in _stored_accounts(bank_session, master_key):
        identification_hash = account.get("identification_hash")
        if (
            identification_hash
            and hash_index(identification_hash, master_key) == link.identification_hash_bidx
        ):
            return account
    return {}


def is_card_account(session: Session, link: BankAccountLink, master_key: str) -> bool:
    """Whether the bank described this account as a card account.

    CashAccountType member, matched by NAME (the contract's enum descriptions
    are misaligned with their values). A card account mirrors the current
    account it debits: it syncs last (R12) and its curve is not reconcilable
    (R19).

    Confirmed on real Boursorama data: the current account carries `CACC`, the
    card account `CARD` (vendor-docs/spike/export-boursorama-2022-2026.json,
    `.accounts[].info`). The field is `required` on `AccountResource`, which is
    what `POST /sessions` returns, so the marker cannot simply be absent — but
    another bank may still label its card account differently, which is what
    `card_marker_missing` on the sync result keeps visible.
    """
    return find_discovered_account(session, link, master_key).get("cash_account_type") == CARD_ACCOUNT_TYPE


def _account_id_label(account: dict[str, Any]) -> str | None:
    """IBAN if the bank gave one, else the "other" identification (BBAN, …)."""
    account_id = account.get("account_id") or {}
    return account_id.get("iban") or (account_id.get("other") or {}).get("identification")


def list_session_accounts(
    session: Session, user_uuid: str, master_key: str, bank_session_uuid: str
) -> list[BankSessionAccount]:
    bank_session, user_bidx = _load_owned_session(session, user_uuid, master_key, bank_session_uuid)
    uuid_by_bidx = _accounts_bank_account_uuid_by_bidx(session, user_bidx, master_key)

    results = []
    for account in _stored_accounts(bank_session, master_key):
        identification_hash = account.get("identification_hash")
        if not identification_hash:
            continue
        link = _find_link_by_ident(
            session, user_bidx, hash_index(identification_hash, master_key)
        )
        results.append(
            BankSessionAccount(
                identification_hash=identification_hash,
                name=account.get("name"),
                product=account.get("product"),
                currency=account.get("currency"),
                cash_account_type=account.get("cash_account_type"),
                usage=account.get("usage"),
                account_id=_account_id_label(account),
                linked=link is not None,
                bank_account_uuid=uuid_by_bidx.get(link.bank_account_uuid_bidx) if link else None,
            )
        )
    return results


def list_bank_sessions(
    session: Session, user_uuid: str, master_key: str
) -> list[BankSessionSummary]:
    """Every authorization this user has granted, newest first.

    Read-only and never gated on the opt-in flag: someone who turns the feature
    back off still has to be able to see what is left attached.
    """
    user_bidx = hash_index(user_uuid, master_key)
    bank_sessions = session.exec(
        select(BankSession)
        .where(BankSession.user_uuid_bidx == user_bidx)
        .order_by(BankSession.authorized_at.desc())  # type: ignore[union-attr]
    ).all()
    if not bank_sessions:
        return []

    account_by_bidx = {
        hash_index(account.uuid, master_key): account
        for account in session.exec(
            select(BankAccount).where(BankAccount.user_uuid_bidx == user_bidx)
        ).all()
    }
    links_by_session: dict[str, list[BankAccountLink]] = {}
    for link in session.exec(
        select(BankAccountLink).where(BankAccountLink.user_uuid_bidx == user_bidx)
    ).all():
        links_by_session.setdefault(link.session_uuid, []).append(link)

    summaries = []
    for bank_session in bank_sessions:
        aspsp = _decrypt_aspsp(bank_session, master_key)
        attached = []
        for link in links_by_session.get(bank_session.uuid, []):
            account = account_by_bidx.get(link.bank_account_uuid_bidx)
            # A link whose CapitalView account is gone: skip rather than invent
            # a name for a row the user can no longer act on.
            if account is None:
                continue
            attached.append(
                BankSessionLinkedAccount(
                    bank_account_uuid=account.uuid,
                    name=decrypt_data(account.name_enc, master_key),
                    last_synced_at=link.last_synced_at,
                )
            )
        attached.sort(key=lambda a: a.name)
        summaries.append(
            BankSessionSummary(
                uuid=bank_session.uuid,
                aspsp_name=aspsp[0] if aspsp else None,
                aspsp_country=aspsp[1] if aspsp else None,
                status=bank_session.status,
                status_message=session_status_message(bank_session.status),
                active=is_session_active(bank_session.status),
                consent_valid_until=bank_session.consent_valid_until,
                authorized_at=bank_session.authorized_at,
                accounts=attached,
            )
        )
    return summaries


def link_account(
    session: Session,
    user_uuid: str,
    master_key: str,
    bank_session_uuid: str,
    identification_hash: str,
    bank_account_uuid: str,
) -> BankAccountLinkResult:
    """Attach (or, on reconnection, re-point) a discovered bank account to a
    CapitalView bank account. The uid is resolved from the session's own stored
    accounts payload, never from a client-supplied value."""
    bank_session, user_bidx = _load_owned_session(session, user_uuid, master_key, bank_session_uuid)

    target_account = session.get(BankAccount, bank_account_uuid)
    if target_account is None or target_account.user_uuid_bidx != user_bidx:
        raise TargetAccountNotFoundError()

    matching_uid = next(
        (
            account.get("uid")
            for account in _stored_accounts(bank_session, master_key)
            if account.get("identification_hash") == identification_hash
        ),
        None,
    )
    if matching_uid is None:
        raise AccountNotFoundInSessionError()

    ident_bidx = hash_index(identification_hash, master_key)
    bank_account_bidx = hash_index(bank_account_uuid, master_key)

    existing_link = _find_link_by_ident(session, user_bidx, ident_bidx)

    reconnected = existing_link is not None
    if existing_link is not None:
        existing_link.session_uuid = bank_session.uuid
        existing_link.account_uid_enc = encrypt_data(matching_uid, master_key)
        existing_link.bank_account_uuid_bidx = bank_account_bidx
        link = existing_link
    else:
        today = date.today()
        current_balance = decrypt_data(target_account.balance_enc, master_key)
        link = BankAccountLink(
            user_uuid_bidx=user_bidx,
            bank_account_uuid_bidx=bank_account_bidx,
            session_uuid=bank_session.uuid,
            identification_hash_bidx=ident_bidx,
            account_uid_enc=encrypt_data(matching_uid, master_key),
            # Bootstrap anchor from the manually-entered CapitalView balance
            # (decision 8: bank data overwrites it on the account's window once
            # Task 6's sync runs). last_synced_at is set before today so the
            # front's daily-sync trigger fires the real fetch right away.
            anchor_date=today,
            anchor_balance_enc=encrypt_data(current_balance, master_key),
            last_synced_at=today - timedelta(days=1),
        )

    session.add(link)
    session.commit()
    session.refresh(link)

    return BankAccountLinkResult(
        bank_account_uuid=bank_account_uuid,
        identification_hash=identification_hash,
        reconnected=reconnected,
    )


# ---------------------------------------------------------------------------
# DELETE /banking/sessions/{uuid} (ruling R3: never exercised against the real
# service in tests — always behind an injected client double)
# ---------------------------------------------------------------------------


def delete_bank_session(session: Session, user_uuid: str, master_key: str, bank_session_uuid: str) -> None:
    bank_session, _ = _load_owned_session(session, user_uuid, master_key, bank_session_uuid)

    # RESTRICT FK (models/banking.py): a session can't be deleted while a link
    # still points at it. A full disconnect means disconnecting its accounts too.
    links = session.exec(
        select(BankAccountLink).where(BankAccountLink.session_uuid == bank_session_uuid)
    ).all()
    for link in links:
        session.delete(link)

    creds = get_decrypted_credentials(session, user_uuid, master_key)
    if creds is not None:
        try:
            session_id = decrypt_data(bank_session.session_id_enc, master_key)
            with build_client(*creds) as client:
                client.close_session(session_id)
        except BankingApiError:
            # Best-effort: the local disconnect still proceeds even if the
            # remote close fails (already closed/expired bank-side, etc).
            pass

    session.delete(bank_session)
    session.commit()
