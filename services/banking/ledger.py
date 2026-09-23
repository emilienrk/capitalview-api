"""
Every stored operation of a user, typed, in one answer: what the Explorer page
filters, groups and compares on its own side, so that changing a filter never
waits on a request.

The reader only filters and adds up. Everything that takes judgement stays
here, computed by the very functions the real cashflow uses: an operation's
type, whether it counts at all (one leg of a pair, a final operation in the
headline currency), and its amount signed in its type's direction. Adding the
`signed` amounts of a completed month's counted rows gives that month's real
cashflow, type by type.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date
from decimal import Decimal

from sqlmodel import Session, select

from dtos.banking import (
    BankLedger,
    BankLedgerAccount,
    BankLedgerGroup,
    BankLedgerRow,
    BankLedgerRecurring,
    BankReviewKind,
    BankTransferStatus,
    CashflowType,
    OperationType,
    TypeSource,
)
from models.banking import BankAccountLink
from services.banking import transfer_patterns as stored_patterns
from services.banking.cashflow_types import counted_leg, signed_amount
from services.banking.flows import (
    _filed,
    _filing,
    _flow_groups,
    _internal_transfer_legs,
    _label,
    _load_movements,
    _pairing,
    _regulated_savings,
    _user_accounts,
)
from services.banking.label_groups import group_key, group_name, group_words, merge_similar
from services.banking.operation_types import operation_type
from services.encryption import decrypt_data, hash_index

# Part of the ETag: a change to how rows are read or grouped must reach a
# browser holding the previous ledger, even though no data moved.
LEDGER_VERSION = "4"


def ledger_etag(session: Session, user_uuid: str, master_key: str) -> str:
    """A fingerprint of everything the ledger is read from, cheap enough to
    answer a conditional request without reading the history: the digest the
    transfer patterns are kept fresh by, plus the accounts as stored."""
    accounts = _user_accounts(session, user_uuid, master_key)
    user_bidx = hash_index(user_uuid, master_key)
    digest = stored_patterns.source_digest(
        session, user_bidx, accounts.readable, _regulated_savings(accounts, master_key), master_key,
    )
    links = sorted(
        (link.bank_account_uuid_bidx, str(link.last_synced_at))
        for link in session.exec(select(BankAccountLink).where(BankAccountLink.user_uuid_bidx == user_bidx)).all()
    )
    stored = sorted(
        (a.uuid, a.name_enc, a.balance_enc, a.account_type_enc, a.institution_name_enc or "", a.currency_enc or "")
        for a in accounts.by_bidx.values()
    )
    return hash_index(json.dumps([LEDGER_VERSION, digest, links, stored]), master_key)


def build_ledger(session: Session, user_uuid: str, master_key: str) -> BankLedger:
    accounts = _user_accounts(session, user_uuid, master_key)
    pairing = _pairing(session, user_uuid, master_key, accounts)
    patterns = pairing.patterns
    movements = _load_movements(session, master_key, accounts.readable, None)
    transfer_legs = _internal_transfer_legs(movements, pairing)
    filing = _filing(session, user_uuid, master_key, accounts, patterns, movements, transfer_legs)
    labels = {index: _label(movement, master_key) for index, movement in enumerate(movements)}
    resolutions = [_filed(movements, transfer_legs, index, labels[index], filing) for index in range(len(movements))]

    counts: dict[str, int] = defaultdict(int)
    for movement in movements:
        if movement.is_final:
            counts[movement.currency] += 1
    currency = max(counts, key=lambda c: counts[c]) if counts else "EUR"

    open_rows: set[int] = set()
    carriers: set[int] = set()
    for members in _flow_groups(movements, transfer_legs, labels, resolutions, patterns.flow_carriers):
        open_rows.update(members)
        carriers.add(members[-1])
    for index, leg in transfer_legs.items():
        if leg.status is BankTransferStatus.SUGGESTED:
            open_rows.add(index)

    recurring_index: dict[str, int] = {}
    recurring: list[BankLedgerRecurring] = []
    asking = {s.carrier for s in patterns.recurring if s.question}

    common = {
        side: frozenset().union(*(patterns.label_common(bidx, side) for bidx in accounts.readable))
        for side in (True, False)
    }
    group_index: dict[tuple[bool, str], int] = {}
    occurrences: list[list[tuple[date | None, str | None]]] = []
    words: list[frozenset[str]] = []
    account_index = {bidx: n for n, bidx in enumerate(accounts.readable)}

    rows: list[BankLedgerRow] = []
    for index, movement in enumerate(movements):
        label = labels[index]
        resolution = resolutions[index]
        leg = transfer_legs.get(index)
        key = (movement.is_credit, group_key(label, common[movement.is_credit]))
        if key not in group_index:
            group_index[key] = len(occurrences)
            occurrences.append([])
            words.append(group_words(label, common[movement.is_credit]))
        occurrences[group_index[key]].append((movement.day, label))

        counted = (
            movement.is_final
            and movement.currency == currency
            and counted_leg(
                movement.is_credit, movement.account_bidx in filing.savings, resolution.type,
                resolution.source is TypeSource.PAIR,
            )
        )
        question = None
        if index in carriers:
            question = BankReviewKind.FLOW
        elif leg is not None and leg.status is BankTransferStatus.SUGGESTED and not movement.is_credit:
            question = BankReviewKind.TRANSFER
        elif movement.row.uuid in asking:
            question = BankReviewKind.RECURRING
        # The rows adding up to "dont … qui reviennent": counted, spent, and a
        # counted recurring payment's.
        stored = patterns.counted_recurring(movement.row.uuid)
        if stored is not None and counted and resolution.type is CashflowType.EXPENSE:
            if stored.key not in recurring_index:
                recurring_index[stored.key] = len(recurring)
                recurring.append(BankLedgerRecurring(
                    id=stored.decision, key=stored.key, name=stored.name, cadence=stored.cadence,
                ))
            in_recurring = recurring_index[stored.key]
        else:
            in_recurring = None
        row = movement.row
        rows.append(BankLedgerRow(
            id=row.uuid,
            account=account_index[movement.account_bidx],
            group=group_index[key],
            day=movement.day,
            amount=movement.amount,
            currency=movement.currency,
            is_credit=movement.is_credit,
            is_pending=not movement.is_final,
            label=label,
            operation_type=(
                OperationType(decrypt_data(row.operation_type_enc, master_key))
                if row.operation_type_enc else operation_type(label)
            ),
            cashflow_type=resolution.type,
            type_source=resolution.source,
            type_rule_id=resolution.rule_id,
            transfer_status=leg.status if leg else None,
            counted=counted,
            signed=signed_amount(movement.amount, movement.is_credit, resolution.type) if counted else Decimal("0"),
            question=question,
            open=index in open_rows,
            recurring=in_recurring,
        ))
    rows.reverse()
    merged, groups = _merge_groups(group_index, occurrences, words)
    for row in rows:
        row.group = merged[row.group]

    links = {
        link.bank_account_uuid_bidx: link.last_synced_at
        for link in session.exec(
            select(BankAccountLink).where(BankAccountLink.user_uuid_bidx == hash_index(user_uuid, master_key))
        ).all()
    }
    ledger_accounts = []
    for bidx in accounts.readable:
        account = accounts.by_bidx[bidx]
        first, last = patterns.coverage.get(bidx, (None, None))
        ledger_accounts.append(BankLedgerAccount(
            id=account.uuid,
            name=decrypt_data(account.name_enc, master_key),
            type=decrypt_data(account.account_type_enc, master_key),
            institution=decrypt_data(account.institution_name_enc, master_key) if account.institution_name_enc else None,
            currency=decrypt_data(account.currency_enc, master_key) if account.currency_enc else currency,
            balance=Decimal(decrypt_data(account.balance_enc, master_key)),
            first_day=first,
            covered_until=links.get(bidx, last),
            linked=bidx in links,
        ))

    return BankLedger(
        currency=currency,
        accounts=ledger_accounts,
        groups=groups,
        rows=rows,
        recurring=recurring,
    )


def _merge_groups(
    group_index: dict[tuple[bool, str], int],
    occurrences: list[list[tuple[date | None, str | None]]],
    words: list[frozenset[str]],
) -> tuple[dict[int, int], list[BankLedgerGroup]]:
    """The groups once the spellings of one counterpart are brought together,
    and where each old group landed."""
    canonical: dict[tuple[bool, str], tuple[bool, str]] = {}
    for is_credit in (True, False):
        side = [
            (key, words[n], len(occurrences[n]))
            for (credit, key), n in group_index.items() if credit is is_credit
        ]
        canonical.update({(is_credit, key): (is_credit, into) for key, into in merge_similar(side).items()})

    moved: dict[int, int] = {}
    kept: dict[tuple[bool, str], int] = {}
    merged_occurrences: list[list[tuple[date | None, str | None]]] = []
    for group, n in sorted(group_index.items(), key=lambda item: item[1]):
        into = canonical[group]
        if into not in kept:
            kept[into] = len(merged_occurrences)
            merged_occurrences.append([])
        moved[n] = kept[into]
        merged_occurrences[kept[into]].extend(occurrences[n])
    return moved, [
        BankLedgerGroup(key=key, name=group_name(merged_occurrences[n]), is_credit=is_credit)
        for (is_credit, key), n in sorted(kept.items(), key=lambda item: item[1])
    ]
