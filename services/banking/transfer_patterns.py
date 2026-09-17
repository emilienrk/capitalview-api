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
pairs are left for the user to settle, month by month, and the flow questions:
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

from models.banking import BankTransaction, BankTransferDecision, BankTransferPatterns, BankTypeRule
from services.encryption import decrypt_data, encrypt_data, hash_index

# A shape that occurred this many times is trusted without asking. Measured:
# at two, a third party refunding two purchases at the same merchant passed for
# a transfer; at three, no false transfer was left, and four only missed more.
RECURRING_MIN_OCCURRENCES = 3

# Bumped whenever what is derived changes, so every stored set is rebuilt.
_VERSION = "8"


class FlowCarrier(NamedTuple):
    # The operations of its label the question settles, and what they add up to.
    count: int
    amount: Decimal


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
    timestamp: an account's type decides whole tiers.
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
    raw = json.dumps(
        [_VERSION, sorted(readable), sorted(savings), list(rows), list(decisions), list(rules)], default=str,
    )
    return hash_index(raw, master_key)


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
