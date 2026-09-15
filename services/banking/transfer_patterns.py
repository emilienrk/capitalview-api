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
pairs are left for the user to settle, month by month, and how often each word
occurs across the history, which category rules are proposed and checked on.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlmodel import Session, select

from models.banking import BankTransaction, BankTransferDecision, BankTransferPatterns
from services.banking.categorize import WordFrequency
from services.encryption import decrypt_data, encrypt_data, hash_index

# A shape that occurred this many times is trusted without asking. Measured:
# at two, a third party refunding two purchases at the same merchant passed for
# a transfer; at three, no false transfer was left, and four only missed more.
RECURRING_MIN_OCCURRENCES = 3

# Bumped whenever what is derived changes, so every stored set is rebuilt.
_VERSION = "4"


@dataclass
class TransferPatterns:
    # "debit account|credit account|debit signature|credit signature" -> count
    shapes: dict[str, int] = field(default_factory=dict)
    # "account|C" or "account|D" -> words too common on that side
    common_words: dict[str, frozenset[str]] = field(default_factory=dict)
    # "YYYY-MM" -> pairs offered to the user and not settled
    questions: dict[str, int] = field(default_factory=dict)
    # How often each word occurs across the signatures, for category rules.
    word_frequency: WordFrequency = field(default_factory=WordFrequency)

    def recurs(
        self, debit_account: str, credit_account: str, debit_signature: str | None, credit_signature: str | None
    ) -> bool:
        if debit_signature is None or credit_signature is None:
            return False
        key = shape_key(debit_account, credit_account, debit_signature, credit_signature)
        return self.shapes.get(key, 0) >= RECURRING_MIN_OCCURRENCES

    def common(self, account: str, is_credit: bool) -> frozenset[str]:
        return self.common_words.get(side_key(account, is_credit), frozenset())


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
    timestamp; so does a decision. The savings accounts are part of it as they
    are, not through a timestamp: an account's type decides whole tiers.
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
    raw = json.dumps([_VERSION, sorted(readable), sorted(savings), list(rows), list(decisions)], default=str)
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
        questions=content["questions"],
        word_frequency=WordFrequency(**content["word_frequency"]),
    )


def write_patterns(
    session: Session, user_bidx: str, source_bidx: str, patterns: TransferPatterns, master_key: str
) -> None:
    content = encrypt_data(
        json.dumps({
            "shapes": patterns.shapes,
            "common_words": {key: sorted(words) for key, words in patterns.common_words.items()},
            "questions": patterns.questions,
            "word_frequency": {
                "counts": patterns.word_frequency.counts,
                "common_above": patterns.word_frequency.common_above,
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
