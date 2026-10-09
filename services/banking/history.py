"""
The history of the user's answers: every decision still in force, newest first,
each one withdrawable (docs/bank-sorting.md).

Read from where each decision already lives — the pair decisions, the rules,
the types forced on operations, the recurring decisions — rather than from a
log of its own: withdrawing a decision deletes it, and every reading of the
operations is back to what it was before, with nothing to keep in step.
"""

from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa
from sqlmodel import Session, select

from dtos.banking import (
    BankHistoryItem,
    BankHistoryKind,
    BankHistoryOperation,
    BankTransferDecisionKind,
    BankTransferStatus,
    CashflowType,
)
from models.banking import BankTransferDecision
from services.banking import recurring as recurring_service
from services.banking.flows import (
    _internal_transfer_legs,
    _label,
    _load_movements,
    _Movement,
    _pairing,
    _user_accounts,
    clear_transaction_type,
    list_type_rules,
)
from services.banking.recurring_decisions import CONFIRMED, RecurringNotFoundError, load_decisions
from services.banking.transfer_decisions import TransactionNotFoundError
from services.banking.type_rules import RuleNotFoundError, delete_rule
from services.encryption import decrypt_data, hash_index

_PAIR_KINDS = {
    BankTransferDecisionKind.TRANSFER: BankHistoryKind.TRANSFER,
    BankTransferDecisionKind.NOT_TRANSFER: BankHistoryKind.NOT_TRANSFER,
    BankTransferDecisionKind.REVERSAL: BankHistoryKind.REVERSAL,
}


class HistoryItemNotFoundError(LookupError):
    """No decision of the user's under this kind and id."""


def action_history(session: Session, user_uuid: str, master_key: str) -> list[BankHistoryItem]:
    accounts = _user_accounts(session, user_uuid, master_key)
    names = {bidx: decrypt_data(account.name_enc, master_key) for bidx, account in accounts.by_bidx.items()}
    pairing = _pairing(session, user_uuid, master_key, accounts)
    movements = _load_movements(session, master_key, accounts.readable, None)
    transfer_legs = _internal_transfer_legs(movements, pairing)
    by_ref = {hash_index(movement.row.uuid, master_key): movement for movement in movements}

    def operation(movement: _Movement) -> BankHistoryOperation:
        return BankHistoryOperation(
            id=movement.row.uuid,
            operation_date=movement.day,
            label=_label(movement, master_key),
            amount=movement.amount,
            currency=movement.currency,
            is_credit=movement.is_credit,
            account_name=names.get(movement.account_bidx, ""),
        )

    items: list[BankHistoryItem] = []
    user_bidx = hash_index(user_uuid, master_key)
    for row in session.exec(select(BankTransferDecision).where(BankTransferDecision.user_uuid_bidx == user_bidx)).all():
        legs = [by_ref[ref] for ref in (row.debit_ref_bidx, row.credit_ref_bidx) if ref in by_ref]
        items.append(BankHistoryItem(
            kind=_PAIR_KINDS[BankTransferDecisionKind(decrypt_data(row.kind_enc, master_key))],
            id=row.uuid,
            at=row.created_at,
            operations=[operation(leg) for leg in legs],
        ))

    for index, movement in enumerate(movements):
        override = movement.row.type_override_enc
        if not override:
            continue
        leg = transfer_legs.get(index)
        items.append(BankHistoryItem(
            kind=BankHistoryKind.TYPE,
            id=movement.row.uuid,
            at=movement.row.type_override_at,
            type=CashflowType(decrypt_data(override, master_key)),
            operations=[operation(movement)],
            overridden_by_pair=leg is not None and leg.status is not BankTransferStatus.SUGGESTED,
        ))

    for rule in list_type_rules(session, user_uuid, master_key):
        items.append(BankHistoryItem(
            kind=BankHistoryKind.RULE,
            id=rule.id,
            at=rule.created_at,
            type=rule.type,
            name=rule.label or rule.signature,
            account_name=rule.account_name,
            operation_count=rule.operation_count,
        ))

    for decision in load_decisions(session, user_uuid, master_key):
        items.append(BankHistoryItem(
            kind=BankHistoryKind.RECURRING,
            id=decision.uuid,
            at=decision.created_at,
            name=decision.name or " ".join(decision.identity.words),
            confirmed=decision.status == CONFIRMED,
        ))

    oldest = datetime.min.replace(tzinfo=timezone.utc)
    items.sort(key=lambda item: _aware(item.at) or oldest, reverse=True)
    return items


def undo_action(session: Session, user_uuid: str, master_key: str, kind: BankHistoryKind, item_id: str) -> None:
    """Withdraw one decision, whatever it was."""
    try:
        if kind is BankHistoryKind.TYPE:
            clear_transaction_type(session, user_uuid, master_key, item_id)
        elif kind is BankHistoryKind.RULE:
            delete_rule(session, user_uuid, master_key, item_id)
        elif kind is BankHistoryKind.RECURRING:
            recurring_service.forget(session, user_uuid, master_key, item_id)
        else:
            deleted = session.exec(
                sa.delete(BankTransferDecision).where(
                    BankTransferDecision.uuid == item_id,
                    BankTransferDecision.user_uuid_bidx == hash_index(user_uuid, master_key),
                )
            )
            if not deleted.rowcount:
                raise HistoryItemNotFoundError(item_id)
            session.commit()
    except (TransactionNotFoundError, RuleNotFoundError, RecurringNotFoundError) as exc:
        raise HistoryItemNotFoundError(item_id) from exc


def _aware(value: datetime | None) -> datetime | None:
    """SQLite hands timestamps back naive: every one was written in UTC."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value
