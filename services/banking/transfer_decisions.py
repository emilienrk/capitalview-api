"""
What the user settled about internal transfers, and what the pairing learns from it.

Amounts and dates cannot tell a transfer from a third party refunding, to the
cent and on the other account, a card purchase made the day before: measured on
real data, that was nearly every false pair left once the pairing itself was
right. So the user decides, and each decision is kept for two uses:

- **the pair itself** — forced into a transfer, kept apart, or bound as a
  movement and its cancellation on one account (a rejected instant transfer, a
  card refund), which no amount-and-date rule can spot among the same-day
  coincidences of an active account;
- **the labels** — each leg's label is reduced to its words, digits dropped,
  and remembered per account and direction as one of the user's own movements
  or not. A later pair whose two legs read like confirmed ones is trusted
  without asking; one with a leg reading like a rejected one is never formed.

No bank's label format is known here: the comparison is between labels of the
same account, and the words both kinds share on that account — a "CARTE" or a
"VIR" prefix — are ignored because they tell nothing apart.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum

import sqlalchemy as sa
from sqlmodel import Session, select

from dtos.banking import BankTransferDecisionKind
from models.banking import BankTransaction, BankTransferDecision
from services.banking.linking import readable_account_bidxs
from services.banking.transactions import CREDIT, label_words, row_date
from services.encryption import decrypt_data, encrypt_data, hash_index

# A leg reads like an exemplar when this share of their informative words match.
# Measured by replaying four years of decisions month by month: lower, a
# "VIR INST <someone else>" passed for the user's own "VIR INST <user>" and true
# transfers were rejected by association; higher, only more questions.
SIMILARITY_THRESHOLD = 0.6

# Two legs further apart than this cannot be bound by hand: every reader loads
# the month either side of the one it shows, and a pair must never have a leg
# outside what a reader loaded.
MAX_DECISION_DAYS = 28

class DecisionError(ValueError):
    """The two movements cannot carry this decision."""


class TransactionNotFoundError(LookupError):
    """No readable movement of this user has this id."""


class Verdict(str, Enum):
    OWN = "own"
    OTHER = "other"


@dataclass(frozen=True)
class _Exemplar:
    # The movement it was read from: a decision proves nothing about its own
    # legs taken one by one — rejecting a pair says one of them is foreign, and
    # the true top-up it was wrongly paired with must stay free to pair again.
    ref_bidx: str
    tokens: frozenset[str]


@dataclass
class _Exemplars:
    own: list[_Exemplar] = field(default_factory=list)
    other: list[_Exemplar] = field(default_factory=list)

    def verdict(self, tokens: frozenset[str], ref_bidx: str | None) -> Verdict | None:
        own = [e.tokens for e in self.own if e.ref_bidx != ref_bidx]
        other = [e.tokens for e in self.other if e.ref_bidx != ref_bidx]
        shared = frozenset().union(*own) & frozenset().union(*other) if own and other else frozenset()
        informative = tokens - shared
        if not informative:
            return None
        best = {Verdict.OWN: 0.0, Verdict.OTHER: 0.0}
        for verdict, exemplars in ((Verdict.OWN, own), (Verdict.OTHER, other)):
            for exemplar in exemplars:
                words = exemplar - shared
                if words:
                    best[verdict] = max(best[verdict], len(informative & words) / len(informative | words))
        own_score, other_score = best[Verdict.OWN], best[Verdict.OTHER]
        if own_score >= SIMILARITY_THRESHOLD and own_score > other_score:
            return Verdict.OWN
        if other_score >= SIMILARITY_THRESHOLD and other_score > own_score:
            return Verdict.OTHER
        return None


@dataclass
class LabelMemory:
    """Exemplar labels, per account blind index and direction."""
    _buckets: dict[tuple[str, bool], _Exemplars] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self._buckets)

    def verdict(
        self, account_bidx: str, is_credit: bool, tokens: frozenset[str], ref_bidx: str | None = None
    ) -> Verdict | None:
        bucket = self._buckets.get((account_bidx, is_credit))
        return bucket.verdict(tokens, ref_bidx) if bucket else None

    def learn(
        self, account_bidx: str, is_credit: bool, tokens: frozenset[str], ref_bidx: str, verdict: Verdict
    ) -> None:
        if not tokens:
            return
        bucket = self._buckets.setdefault((account_bidx, is_credit), _Exemplars())
        (bucket.own if verdict is Verdict.OWN else bucket.other).append(_Exemplar(ref_bidx, tokens))


@dataclass
class Decisions:
    # (debit ref bidx, credit ref bidx) -> TRANSFER or REVERSAL
    bound: dict[tuple[str, str], BankTransferDecisionKind] = field(default_factory=dict)
    rejected: set[tuple[str, str]] = field(default_factory=set)
    memory: LabelMemory = field(default_factory=LabelMemory)

    def __bool__(self) -> bool:
        return bool(self.bound or self.rejected)


def load_decisions(session: Session, user_uuid: str, master_key: str) -> Decisions:
    """Every decision of the user, replayed in the order they were taken.

    A rejected pair teaches that one of its legs is not the user's own — not
    which. The leg that already reads like a confirmed one is taken to be the
    user's; the other, or both when neither does, is remembered as foreign.
    Replayed rather than stored, so withdrawing a decision withdraws what it
    taught.
    """
    rows = session.exec(
        select(BankTransferDecision)
        .where(BankTransferDecision.user_uuid_bidx == hash_index(user_uuid, master_key))
        .order_by(BankTransferDecision.created_at, BankTransferDecision.uuid)
    ).all()
    decisions = Decisions()
    for row in rows:
        kind = BankTransferDecisionKind(decrypt_data(row.kind_enc, master_key))
        pair = (row.debit_ref_bidx, row.credit_ref_bidx)
        legs = (
            (row.debit_account_bidx, False, _tokens(row.debit_tokens_enc, master_key), row.debit_ref_bidx),
            (row.credit_account_bidx, True, _tokens(row.credit_tokens_enc, master_key), row.credit_ref_bidx),
        )
        if kind is BankTransferDecisionKind.NOT_TRANSFER:
            decisions.rejected.add(pair)
            # Undoing a cancellation on one account says nothing about transfers.
            if row.debit_account_bidx == row.credit_account_bidx:
                continue
            for account, is_credit, tokens, ref in legs:
                if decisions.memory.verdict(account, is_credit, tokens, ref) is not Verdict.OWN:
                    decisions.memory.learn(account, is_credit, tokens, ref, Verdict.OTHER)
            continue
        decisions.bound[pair] = kind
        # A cancellation says nothing about transfers.
        if kind is BankTransferDecisionKind.TRANSFER:
            for account, is_credit, tokens, ref in legs:
                decisions.memory.learn(account, is_credit, tokens, ref, Verdict.OWN)
    return decisions


def _tokens(value: str, master_key: str) -> frozenset[str]:
    return frozenset(json.loads(decrypt_data(value, master_key)))


@dataclass(frozen=True)
class _Leg:
    row: BankTransaction
    account_bidx: str
    is_credit: bool
    amount: Decimal
    currency: str
    label: str | None


def _readable_leg(
    session: Session, transaction_id: str, readable: set[str], master_key: str
) -> _Leg:
    row = session.get(BankTransaction, transaction_id)
    if row is None or row.account_id_bidx not in readable:
        raise TransactionNotFoundError(transaction_id)
    return _Leg(
        row=row,
        account_bidx=row.account_id_bidx,
        is_credit=decrypt_data(row.credit_debit_enc, master_key) == CREDIT,
        amount=Decimal(decrypt_data(row.amount_enc, master_key)),
        currency=decrypt_data(row.currency_enc, master_key),
        label=decrypt_data(row.remittance_enc, master_key) if row.remittance_enc else None,
    )


def record_decision(
    session: Session,
    user_uuid: str,
    master_key: str,
    first_id: str,
    second_id: str,
    kind: BankTransferDecisionKind,
) -> None:
    """Settle two movements, replacing whatever was decided about them before.

    Binding them — as a transfer or a cancellation — also drops any other pair
    either leg was bound into: a movement is one leg of one pair at most.
    """
    user_bidx = hash_index(user_uuid, master_key)
    readable = set(readable_account_bidxs(session, user_bidx, master_key))
    first = _readable_leg(session, first_id, readable, master_key)
    second = _readable_leg(session, second_id, readable, master_key)

    if first.is_credit == second.is_credit:
        raise DecisionError("Les deux opérations doivent aller en sens opposés.")
    debit, credit = (second, first) if first.is_credit else (first, second)
    if debit.amount != credit.amount or debit.currency != credit.currency:
        raise DecisionError("Les deux opérations doivent porter le même montant, dans la même devise.")
    same_account = debit.account_bidx == credit.account_bidx
    if kind is BankTransferDecisionKind.REVERSAL and not same_account:
        raise DecisionError("Une annulation se lit sur un seul compte.")
    if kind is BankTransferDecisionKind.TRANSFER and same_account:
        raise DecisionError("Un virement interne relie deux comptes différents.")
    debit_day, credit_day = row_date(debit.row, master_key), row_date(credit.row, master_key)
    if debit_day is None or credit_day is None or abs((credit_day - debit_day).days) > MAX_DECISION_DAYS:
        raise DecisionError(f"Les deux opérations doivent être à moins de {MAX_DECISION_DAYS} jours d'écart.")

    debit_ref = hash_index(debit.row.uuid, master_key)
    credit_ref = hash_index(credit.row.uuid, master_key)
    same_user = BankTransferDecision.user_uuid_bidx == user_bidx
    pair = sa.and_(
        BankTransferDecision.debit_ref_bidx == debit_ref,
        BankTransferDecision.credit_ref_bidx == credit_ref,
    )
    session.exec(sa.delete(BankTransferDecision).where(same_user, pair))
    if kind is not BankTransferDecisionKind.NOT_TRANSFER:
        for existing in session.exec(
            select(BankTransferDecision).where(
                same_user,
                sa.or_(
                    BankTransferDecision.debit_ref_bidx == debit_ref,
                    BankTransferDecision.credit_ref_bidx == credit_ref,
                ),
            )
        ).all():
            existing_kind = BankTransferDecisionKind(decrypt_data(existing.kind_enc, master_key))
            if existing_kind is not BankTransferDecisionKind.NOT_TRANSFER:
                session.delete(existing)

    session.add(
        BankTransferDecision(
            user_uuid_bidx=user_bidx,
            kind_enc=encrypt_data(kind.value, master_key),
            debit_ref_bidx=debit_ref,
            credit_ref_bidx=credit_ref,
            debit_account_bidx=debit.account_bidx,
            credit_account_bidx=credit.account_bidx,
            debit_tokens_enc=encrypt_data(json.dumps(sorted(label_words(debit.label))), master_key),
            credit_tokens_enc=encrypt_data(json.dumps(sorted(label_words(credit.label))), master_key),
            created_at=datetime.now(timezone.utc),
        )
    )
    session.commit()

