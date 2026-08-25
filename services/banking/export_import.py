"""
Ingestion of Enable Banking JSON export files (catch-up / historical import).

Enable Banking allows exporting the entire transaction history as a JSON file,
independent of the 1-hour/90-day banking window. The export structure mirrors
the API payloads directly:
  {"accounts": [{"info": {...}, "transactions": [...], "balances": [...]}]}

Transactions are normalized via `normalize_transaction` and stored via
`store_transactions` from Task 5, using durable `identification_hash` matching.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from sqlmodel import Session, select

from dtos.bank import BankHistoryEntry
from dtos.banking import BankExportImportResponse, BankExportImportResult
from models.bank import BankAccount
from models.banking import BankAccountLink, BankSession
from services.bank import replace_history_window
from services.banking.linking import is_card_account
from services.banking.sync import (
    AccountingBalanceUnavailableError,
    _accounting_balance_row,
    _booked_movements,
    _curve_entries,
)
from services.banking.transactions import (
    NormalizedTransaction,
    normalize_transaction,
    store_transactions,
)
from services.encryption import decrypt_data, encrypt_data, hash_index

logger = logging.getLogger(__name__)


def import_enablebanking_export(
    session: Session,
    user_uuid: str,
    master_key: str,
    payload: dict[str, Any] | list[dict[str, Any]],
) -> BankExportImportResponse:
    """Import an Enable Banking JSON export for the authenticated user.

    Accounts in the export are matched against the user's `BankAccountLink`s
    by `identification_hash` (the durable attachment key).
    """
    user_bidx = hash_index(user_uuid, master_key)

    if isinstance(payload, dict):
        accounts_data = payload.get("accounts")
        if accounts_data is None:
            raise ValueError("Le fichier d'export ne contient pas de liste 'accounts'.")
    elif isinstance(payload, list):
        accounts_data = payload
    else:
        raise ValueError("Format d'export Enable Banking non reconnu.")

    if not isinstance(accounts_data, list):
        raise ValueError("La liste 'accounts' doit être un tableau JSON.")

    # Load all bank account links for this user
    links = session.exec(
        select(BankAccountLink).where(BankAccountLink.user_uuid_bidx == user_bidx)
    ).all()

    # Map identification_hash_bidx -> link
    link_by_ident_bidx = {link.identification_hash_bidx: link for link in links}

    # Map bank_account_uuid_bidx -> BankAccount
    user_accounts = session.exec(
        select(BankAccount).where(BankAccount.user_uuid_bidx == user_bidx)
    ).all()
    account_by_bidx = {
        hash_index(acc.uuid, master_key): acc for acc in user_accounts
    }

    results: list[BankExportImportResult] = []
    imported_count = 0

    for item in accounts_data:
        info = item.get("info") or {}
        ident_hash = info.get("identification_hash") or item.get("identification_hash")
        ident_hashes = info.get("identification_hashes") or (
            [ident_hash] if ident_hash else []
        )

        matched_link: BankAccountLink | None = None
        for h in ident_hashes:
            h_bidx = hash_index(h, master_key)
            if h_bidx in link_by_ident_bidx:
                matched_link = link_by_ident_bidx[h_bidx]
                break

        if matched_link is None:
            account_label = (
                (info.get("account_id") or {}).get("iban")
                or ident_hash
                or "Compte inconnu"
            )
            results.append(
                BankExportImportResult(
                    bank_account_uuid="unlinked",
                    status="unlinked",
                    detail=f"Aucun compte CapitalView n'est rattaché à {account_label}.",
                )
            )
            continue

        target_account = account_by_bidx.get(matched_link.bank_account_uuid_bidx)
        if target_account is None:
            results.append(
                BankExportImportResult(
                    bank_account_uuid="not_found",
                    status="error",
                    detail="Le compte CapitalView lié est introuvable.",
                )
            )
            continue

        # Process transactions
        raw_txs = item.get("transactions") or []
        parsed: list[NormalizedTransaction] = []
        malformed = 0

        for raw in raw_txs:
            try:
                parsed.append(normalize_transaction(raw))
            except Exception:
                malformed += 1

        inserted, updated, skipped = store_transactions(
            session, user_uuid, master_key, target_account.uuid, raw_txs
        )

        # Handle balances and history curve
        raw_balances = item.get("balances") or []
        snapshots_written = 0
        status = "imported"
        detail = None
        is_card = is_card_account(session, matched_link, master_key)

        if is_card:
            detail = (
                "Courbe rétrospective non écrite : les mouvements de ce compte sont "
                "dédupliqués vers le compte courant (ruling R19)."
            )
        elif not raw_balances:
            detail = "Aucun solde dans l'export : courbe rétrospective non écrite."
        elif not parsed:
            detail = "Aucune transaction exploitable : courbe rétrospective non écrite."
        else:
            try:
                snapshots_written, detail = _write_export_curve(
                    session, master_key, matched_link, target_account, raw_balances, parsed
                )
            except AccountingBalanceUnavailableError as exc:
                # A distinct status, never "imported" with a silent zero: that
                # is indistinguishable from a card account doing the right thing.
                logger.warning("export import: %s (account %s)", exc, target_account.uuid)
                status = "balance_unavailable"
                detail = (
                    "Aucun solde comptable en euros dans l'export : les opérations sont "
                    "importées, la courbe rétrospective ne l'est pas."
                )
            except Exception:
                logger.exception(
                    "export import: balance curve build failed for account %s",
                    target_account.uuid,
                )
                status = "curve_error"
                detail = (
                    "Les opérations sont importées, mais la courbe rétrospective "
                    "n'a pas pu être reconstruite."
                )

        results.append(
            BankExportImportResult(
                bank_account_uuid=target_account.uuid,
                status=status,
                inserted=inserted,
                updated=updated,
                skipped=skipped,
                malformed=malformed,
                snapshots_written=snapshots_written,
                detail=detail,
            )
        )
        imported_count += 1

    return BankExportImportResponse(imported_accounts=imported_count, results=results)


def _write_export_curve(
    session: Session,
    master_key: str,
    link: BankAccountLink,
    account: BankAccount,
    raw_balances: list[dict[str, Any]],
    parsed: list[NormalizedTransaction],
) -> tuple[int, str | None]:
    """Rebuild the balance curve over the window the export covers.

    Returns the number of snapshots written and a detail line, if any.
    """
    # The same reading the sync uses (§F, constraint 9): the accounting balance,
    # matched by type. Falling back to `balances[0]` would take the real-time
    # balance one time in two.
    balance_row = _accounting_balance_row({"balances": raw_balances})
    bal_amount = Decimal(str((balance_row.get("balance_amount") or {}).get("amount")))
    ref_date_str = balance_row.get("reference_date")
    ref_date = date.fromisoformat(ref_date_str) if ref_date_str else date.today()

    valid_dates = [tx.effective_date for tx in parsed if tx.effective_date is not None]
    if not valid_dates:
        return 0, "Aucune opération datable dans l'export : courbe rétrospective non écrite."
    covered_from = min(valid_dates)

    movements = _booked_movements(session, account, master_key, covered_from, ref_date)
    snapshots_written = replace_history_window(
        session,
        account,
        _curve_entries(bal_amount, movements, covered_from, ref_date),
        master_key,
        covered_from,
        ref_date,
    )

    # An export is a *catch-up*: its reference date is routinely older than what
    # the link already knows. The curve above is bounded to [covered_from,
    # ref_date] and is history, so it is written either way — but the anchor,
    # `last_synced_at` and the account's current balance say "where we are now".
    # Walking them backwards restores a stale balance as the current one (the
    # trap §D5 names) and, because R7 derives "estimated" from `anchor_date`,
    # silently re-labels already reconciled days as estimated.
    detail: str | None = None
    if ref_date < link.anchor_date:
        detail = (
            f"Export antérieur à l'ancre du compte ({link.anchor_date.isoformat()}) : "
            "courbe rétrospective écrite, ancre et solde courant conservés."
        )
    else:
        link.anchor_date = ref_date
        link.anchor_balance_enc = encrypt_data(
            str(bal_amount - movements.get(ref_date, Decimal("0"))), master_key
        )
        link.last_synced_at = max(link.last_synced_at, ref_date)
        session.add(link)

    if account.balance_updated_at is None or ref_date >= account.balance_updated_at:
        account.balance_enc = encrypt_data(str(bal_amount), master_key)
        account.balance_updated_at = ref_date
        session.add(account)

    session.commit()
    return snapshots_written, detail
