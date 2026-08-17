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
    ACCOUNTING_BALANCE_TYPE,
    _accounting_balance,
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
        is_card = is_card_account(session, matched_link, master_key)

        if raw_balances and not is_card and parsed:
            # Pick closing balance (CLBD) if available, otherwise first balance
            clbd = next(
                (b for b in raw_balances if (b.get("balance_type") or "").upper() == ACCOUNTING_BALANCE_TYPE),
                raw_balances[0],
            )
            try:
                bal_amount = Decimal(str(clbd["balance_amount"]["amount"]))
                ref_date_str = clbd.get("reference_date")
                ref_date = date.fromisoformat(ref_date_str) if ref_date_str else date.today()

                valid_dates = [t.effective_date for t in parsed if t.effective_date is not None]
                if valid_dates:
                    covered_from = min(valid_dates)
                    movements = _booked_movements(
                        session, target_account, master_key, covered_from, ref_date
                    )
                    curve_entries = _curve_entries(bal_amount, movements, covered_from, ref_date)
                    snapshots_written = replace_history_window(
                        session,
                        target_account,
                        curve_entries,
                        master_key,
                        covered_from,
                        ref_date,
                    )

                    # Update anchor and account balance
                    matched_link.anchor_date = ref_date
                    matched_link.anchor_balance_enc = encrypt_data(
                        str(bal_amount - movements.get(ref_date, Decimal("0"))), master_key
                    )
                    matched_link.last_synced_at = ref_date
                    session.add(matched_link)

                    target_account.balance_enc = encrypt_data(str(bal_amount), master_key)
                    target_account.balance_updated_at = ref_date
                    session.add(target_account)
                    session.commit()
            except Exception as e:
                logger.exception("Failed to build balance curve from export balances: %s", e)

        detail = None
        if is_card:
            detail = (
                "Courbe rétrospective non écrite : les mouvements de ce compte sont "
                "dédupliqués vers le compte courant (ruling R19)."
            )

        results.append(
            BankExportImportResult(
                bank_account_uuid=target_account.uuid,
                status="imported",
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
