"""
What a user's whole history says about internal transfers, kept between reads.

Amounts and dates alone cannot tell a transfer between two current accounts
from a third party refunding the exact amount of a purchase on the other one.
What does tell them apart, measured on four years of real movements, is
repetition: a top-up from one account to the other comes back dozens of times
under the same pair of labels, while each refund pairs a different merchant
with it. So a pair is trusted once its *shape* — the two accounts and the two
label signatures — has occurred often enough; a pair seen once is only offered
to the user.

Counting shapes takes the whole history, which a month's reader never loads.
The counts are therefore derived once and stored, with a digest of what they
were derived from. Nothing updates them in place: a reader that finds the
digest outdated rebuilds them before reading (`flows.transfer_patterns`), so a
sync, an import, a deletion or a decision can never leave them stale, whichever
path wrote it.

Stored alongside, from the same pass: the words too common on each side of an
account to tell a refund from its purchase ("CARTE", "CB", "VIR"), how many
pairs are left for the user to settle, month by month, the recurring payments
(services/banking/recurring_series.py), and the flow questions:
which operation of each label nothing types but the user carries its question,
since a month's reader cannot tell which occurrence of a label is the last —
with the amounts still open, so a reader can say what an answer may move — and
the span of days each account's history covers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import NamedTuple

import sqlalchemy as sa
from sqlmodel import Session, select

from dtos.banking import CashflowType, RecurringDirection
from models.banking import (
    BankRecurringSeries,
    BankTransaction,
    BankTransferDecision,
    BankTransferPatterns,
    BankTypeRule,
)
from models.crypto import CryptoAccount, CryptoTransaction
from models.stock import StockAccount, StockTransaction
from services.encryption import decrypt_data, encrypt_data, hash_index

# A shape that occurred this many times is trusted without asking. Measured:
# at two, a third party refunding two purchases at the same merchant passed for
# a transfer; at three, no false transfer was left, and four only missed more.
RECURRING_MIN_OCCURRENCES = 3

# Bumped whenever what is derived changes, so every stored set is rebuilt.
_VERSION = "14"


class FlowCarrier(NamedTuple):
    # The operations of its label the question settles, and what they add up to.
    count: int
    amount: Decimal


# What a member is to its recurring payment or income. Only the first three and a
# refund count towards "dont … qui reviennent", and only when typed as its kind.
REGULAR, EXTRA, MANUAL, CANCELLED, REFUND = "regular", "extra", "manual", "cancelled", "refund"
COUNTED_ROLES = frozenset({REGULAR, EXTRA, MANUAL, REFUND})


class RecurringMember(NamedTuple):
    uuid: str
    role: str
    day: date
    amount: Decimal
    is_credit: bool
    # Typed as its recurring's kind (EXPENSE or INCOME) when the patterns were built.
    typed: bool
    # Kept for a refund only, which the recurring payment shows by its label.
    label: str | None = None


@dataclass
class StoredRecurring:
    """One recurring payment or income as the rebuild found it
    (services/banking/recurring_series.py).

    `key` is the decision's id when the user decided, else the id of its first
    debit: stable while the series keeps that debit, which is all a reader
    holding it between two rebuilds needs.
    """
    key: str
    decision: str | None
    # expense | income
    direction: str
    # auto | confirmed | candidate | refused
    state: str
    confidence: str | None
    cadence: str
    variable: bool
    currency: str
    members: list[RecurringMember]
    # (start, end, amount, count), oldest first.
    levels: list[tuple[date, date, Decimal, int]]
    episodes: list[tuple[date, date]]
    first: date
    last: date
    amount: Decimal
    name: str
    renamed: list[tuple[date, str, str]]
    accounts: list[str]
    # The account of the last regular debit: whose coverage says whether it still runs.
    last_account: str
    method: str
    # The operation a question sits on, and whether it asks.
    carrier: str | None
    question: bool
    counted: bool
    # The merchant's (or payer's) words, to record as a decision's identity.
    words: list[str]
    # What the user filed it as; None until they say.
    nature: str | None = None
    ended_on: date | None = None

    @property
    def kind(self) -> CashflowType:
        """The type its operations count as."""
        return CashflowType.INCOME if self.direction == RecurringDirection.INCOME.value else CashflowType.EXPENSE

    def to_json(self) -> dict:
        return {
            "key": self.key, "decision": self.decision, "direction": self.direction,
            "state": self.state, "confidence": self.confidence,
            "cadence": self.cadence, "variable": self.variable, "currency": self.currency,
            "members": [
                [m.uuid, m.role, m.day.isoformat(), str(m.amount), m.is_credit, m.typed, m.label]
                for m in self.members
            ],
            "levels": [[a.isoformat(), b.isoformat(), str(amount), count] for a, b, amount, count in self.levels],
            "episodes": [[a.isoformat(), b.isoformat()] for a, b in self.episodes],
            "first": self.first.isoformat(), "last": self.last.isoformat(), "amount": str(self.amount),
            "name": self.name, "renamed": [[day.isoformat(), before, after] for day, before, after in self.renamed],
            "accounts": self.accounts, "last_account": self.last_account, "method": self.method,
            "carrier": self.carrier, "question": self.question, "counted": self.counted, "words": self.words,
            "nature": self.nature,
            "ended_on": self.ended_on.isoformat() if self.ended_on else None,
        }

    @classmethod
    def from_json(cls, content: dict) -> StoredRecurring:
        return cls(
            key=content["key"], decision=content["decision"], direction=content["direction"], state=content["state"],
            confidence=content["confidence"], cadence=content["cadence"], variable=content["variable"],
            currency=content["currency"],
            members=[
                RecurringMember(uuid, role, date.fromisoformat(day), Decimal(amount), is_credit, typed, label)
                for uuid, role, day, amount, is_credit, typed, label in content["members"]
            ],
            levels=[
                (date.fromisoformat(a), date.fromisoformat(b), Decimal(amount), count)
                for a, b, amount, count in content["levels"]
            ],
            episodes=[(date.fromisoformat(a), date.fromisoformat(b)) for a, b in content["episodes"]],
            first=date.fromisoformat(content["first"]), last=date.fromisoformat(content["last"]),
            amount=Decimal(content["amount"]), name=content["name"],
            renamed=[(date.fromisoformat(day), before, after) for day, before, after in content["renamed"]],
            accounts=content["accounts"], last_account=content["last_account"], method=content["method"],
            carrier=content["carrier"], question=content["question"], counted=content["counted"],
            words=content["words"],
            nature=content["nature"],
            ended_on=date.fromisoformat(content["ended_on"]) if content["ended_on"] else None,
        )


@dataclass
class TransferPatterns:
    # "debit account|credit account|debit signature|credit signature" -> count
    shapes: dict[str, int] = field(default_factory=dict)
    # "account|C" or "account|D" -> words too common on that side
    common_words: dict[str, frozenset[str]] = field(default_factory=dict)
    # Same keys -> words found in too many distinct labels of that side
    label_common_words: dict[str, frozenset[str]] = field(default_factory=dict)
    # "YYYY-MM" -> pairs offered to the user and not settled
    questions: dict[str, int] = field(default_factory=dict)
    # Same keys -> the amount of those pairs, counted once
    questions_amount: dict[str, Decimal] = field(default_factory=dict)
    # Operation uuid -> the operations of its label its flow question settles
    flow_carriers: dict[str, FlowCarrier] = field(default_factory=dict)
    # "YYYY-MM" -> flow questions carried by an operation of that month
    flow_questions: dict[str, int] = field(default_factory=dict)
    # "YYYY-MM" -> operations of that month waiting on a flow question
    flow_open: dict[str, int] = field(default_factory=dict)
    # Same keys -> the amount of those operations
    flow_open_amount: dict[str, Decimal] = field(default_factory=dict)
    # Account blind index -> (first, last) day of its stored operations
    coverage: dict[str, tuple[date, date]] = field(default_factory=dict)
    # Every series offered, counted or decided (services/banking/recurring_series.py).
    recurring: list[StoredRecurring] = field(default_factory=list)
    # "YYYY-MM" -> recurring payment and income questions carried by an operation of that month
    recurring_questions: dict[str, int] = field(default_factory=dict)
    _members: dict[str, tuple[StoredRecurring, RecurringMember]] | None = field(default=None, repr=False)

    def recurring_of(self, uuid: str) -> tuple[StoredRecurring, RecurringMember] | None:
        """The recurring payment an operation belongs to, and as what."""
        if self._members is None:
            self._members = {
                member.uuid: (stored, member)
                for stored in self.recurring for member in stored.members
            }
        return self._members.get(uuid)

    def counted_recurring(self, uuid: str) -> StoredRecurring | None:
        """The recurring payment or income this operation counts in, whatever
        its type: the reader still checks it carries the recurring's kind."""
        found = self.recurring_of(uuid)
        if found is None:
            return None
        stored, member = found
        return stored if stored.counted and member.role in COUNTED_ROLES else None

    def recurs(
        self, debit_account: str, credit_account: str, debit_signature: str | None, credit_signature: str | None
    ) -> bool:
        if debit_signature is None or credit_signature is None:
            return False
        key = shape_key(debit_account, credit_account, debit_signature, credit_signature)
        return self.shapes.get(key, 0) >= RECURRING_MIN_OCCURRENCES

    def common(self, account: str, is_credit: bool) -> frozenset[str]:
        return self.common_words.get(side_key(account, is_credit), frozenset())

    def label_common(self, account: str, is_credit: bool) -> frozenset[str]:
        return self.label_common_words.get(side_key(account, is_credit), frozenset())


def shape_key(debit_account: str, credit_account: str, debit_signature: str, credit_signature: str) -> str:
    return f"{debit_account}|{credit_account}|{debit_signature}|{credit_signature}"


def side_key(account: str, is_credit: bool) -> str:
    return f"{account}|{'C' if is_credit else 'D'}"


def source_digest(
    session: Session, user_bidx: str, readable: list[str], savings: frozenset[str], master_key: str
) -> str:
    """A fingerprint of everything the patterns are derived from.

    Cheap on purpose — counts and timestamps, no row decrypted — since every
    read computes it. Any row added, removed or rewritten moves a count or a
    timestamp; so do a decision and a type rule, which is replaced rather than
    updated. The savings accounts are part of it as they are, not through a
    timestamp: an account's type decides whole tiers. A recurring payment decision
    is updated in place, so its latest update time counts, not its creation.

    The investment accounts count too: a deposit declared on one of them types
    the transfer that fed it, so saving one settles a question
    (services/banking/contributions.py).
    """
    rows = (0, None, None)
    if readable:
        rows = session.exec(
            select(
                sa.func.count(),
                sa.func.max(BankTransaction.updated_at),
                sa.func.max(BankTransaction.created_at),
            ).where(BankTransaction.account_id_bidx.in_(readable))  # type: ignore[attr-defined]
        ).one()
    decisions = session.exec(
        select(sa.func.count(), sa.func.max(BankTransferDecision.created_at)).where(
            BankTransferDecision.user_uuid_bidx == user_bidx
        )
    ).one()
    rules = session.exec(
        select(sa.func.count(), sa.func.max(BankTypeRule.created_at)).where(BankTypeRule.user_uuid_bidx == user_bidx)
    ).one()
    recurring = session.exec(
        select(sa.func.count(), sa.func.max(BankRecurringSeries.updated_at)).where(
            BankRecurringSeries.user_uuid_bidx == user_bidx
        )
    ).one()
    investments = _investment_rows(session, user_bidx, master_key)
    raw = json.dumps(
        [
            _VERSION, sorted(readable), sorted(savings),
            list(rows), list(decisions), list(rules), list(recurring), investments,
        ],
        default=str,
    )
    return hash_index(raw, master_key)


def _investment_rows(session: Session, user_bidx: str, master_key: str) -> list[list]:
    """Counts and timestamps of the user's stock and crypto transactions."""
    signature: list[list] = []
    for account_model, row_model in ((StockAccount, StockTransaction), (CryptoAccount, CryptoTransaction)):
        bidxs = [
            hash_index(account.uuid, master_key)
            for account in session.exec(
                select(account_model).where(account_model.user_uuid_bidx == user_bidx)
            ).all()
        ]
        if not bidxs:
            signature.append([0, None, None])
            continue
        counted = session.exec(
            select(
                sa.func.count(),
                sa.func.max(row_model.updated_at),
                sa.func.max(row_model.created_at),
            ).where(row_model.account_id_bidx.in_(bidxs))  # type: ignore[attr-defined]
        ).one()
        signature.append(list(counted))
    return signature


def read_patterns(
    session: Session, user_bidx: str, source_bidx: str, master_key: str
) -> TransferPatterns | None:
    """The stored patterns, or None when they are missing or were built from
    other data than there is now."""
    row = session.get(BankTransferPatterns, user_bidx)
    if row is None or row.source_bidx != source_bidx:
        return None
    content = json.loads(decrypt_data(row.content_enc, master_key))
    return TransferPatterns(
        shapes=content["shapes"],
        common_words={key: frozenset(words) for key, words in content["common_words"].items()},
        label_common_words={key: frozenset(words) for key, words in content["label_common_words"].items()},
        questions=content["questions"],
        questions_amount=_amounts(content["questions_amount"]),
        flow_carriers={
            uuid: FlowCarrier(count, Decimal(amount)) for uuid, (count, amount) in content["flow_carriers"].items()
        },
        flow_questions=content["flow_questions"],
        flow_open=content["flow_open"],
        flow_open_amount=_amounts(content["flow_open_amount"]),
        coverage={
            account: (date.fromisoformat(first), date.fromisoformat(last))
            for account, (first, last) in content["coverage"].items()
        },
        recurring=[StoredRecurring.from_json(item) for item in content["recurring"]],
        recurring_questions=content["recurring_questions"],
    )


def write_patterns(
    session: Session, user_bidx: str, source_bidx: str, patterns: TransferPatterns, master_key: str
) -> None:
    content = encrypt_data(
        json.dumps({
            "shapes": patterns.shapes,
            "common_words": {key: sorted(words) for key, words in patterns.common_words.items()},
            "label_common_words": {key: sorted(words) for key, words in patterns.label_common_words.items()},
            "questions": patterns.questions,
            "questions_amount": {period: str(amount) for period, amount in patterns.questions_amount.items()},
            "flow_carriers": {
                uuid: [carrier.count, str(carrier.amount)] for uuid, carrier in patterns.flow_carriers.items()
            },
            "flow_questions": patterns.flow_questions,
            "flow_open": patterns.flow_open,
            "flow_open_amount": {period: str(amount) for period, amount in patterns.flow_open_amount.items()},
            "coverage": {
                account: [first.isoformat(), last.isoformat()] for account, (first, last) in patterns.coverage.items()
            },
            "recurring": [stored.to_json() for stored in patterns.recurring],
            "recurring_questions": patterns.recurring_questions,
        }),
        master_key,
    )
    row = session.get(BankTransferPatterns, user_bidx)
    if row is None:
        row = BankTransferPatterns(user_uuid_bidx=user_bidx, source_bidx=source_bidx, content_enc=content,
                                   built_at=datetime.now(timezone.utc))
        session.add(row)
    else:
        row.source_bidx = source_bidx
        row.content_enc = content
        row.built_at = datetime.now(timezone.utc)
    session.commit()


def _amounts(content: dict[str, str]) -> dict[str, Decimal]:
    return {period: Decimal(amount) for period, amount in content.items()}
