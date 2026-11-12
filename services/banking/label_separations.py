"""
The labels the user separated: one identity holding operations of different
natures.

A label's identity is its words (`labels.label_signature`). Where only the
bank's own format is left of them — "CARTE 12/03/25 A.R.E.A. CB*0837" reads
"carte cb" — payments to different merchants share it, and an answer given
for the label would type them all. No list of words may tell them apart
(docs/bank-sorting.md, R1): the user does, by separating the label on its
account and direction. Each of its operations then asks alone, and no rule
given from an answer reaches it, its own or a nearby one's; a rule written by
hand still does, being the user's word.

Kept apart from the operations, read as they are read, never written onto
them: a separation also holds for the operations imported later, and
regrouping undoes it with nothing to repair.

Rules: docs/bank-sorting.md — R54.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlmodel import Session, select

from models.banking import BankLabelSeparation
from services.encryption import decrypt_data, encrypt_data, hash_index


class SeparationNotFoundError(LookupError):
    """No separation of this user has this id."""


@dataclass(frozen=True)
class Separation:
    uuid: str
    account_bidx: str
    account_id: str
    is_credit: bool
    signature: str
    created_at: datetime

    @property
    def key(self) -> tuple[str, bool, str]:
        return self.account_bidx, self.is_credit, self.signature


def separation_bidx(account_id: str, is_credit: bool, signature: str, master_key: str) -> str:
    return hash_index(f"label-separation:{account_id}:{_flag(is_credit)}:{signature}", master_key)


def load_separations(session: Session, user_uuid: str, master_key: str) -> list[Separation]:
    rows = session.exec(
        select(BankLabelSeparation).where(BankLabelSeparation.user_uuid_bidx == hash_index(user_uuid, master_key))
    ).all()
    return [
        Separation(
            uuid=row.uuid,
            account_bidx=row.account_bidx,
            account_id=decrypt_data(row.account_ref_enc, master_key),
            is_credit=decrypt_data(row.credit_enc, master_key) == _flag(True),
            signature=decrypt_data(row.signature_enc, master_key),
            created_at=row.created_at,
        )
        for row in rows
    ]


def save_separation(
    session: Session, user_uuid: str, master_key: str, account_id: str, is_credit: bool, signature: str,
) -> BankLabelSeparation:
    """Separate a label, or hand back its separation when it already is.
    Left to the caller to commit, with the rule it withdraws."""
    user_bidx = hash_index(user_uuid, master_key)
    bidx = separation_bidx(account_id, is_credit, signature, master_key)
    existing = session.exec(
        select(BankLabelSeparation).where(
            BankLabelSeparation.user_uuid_bidx == user_bidx, BankLabelSeparation.separation_bidx == bidx,
        )
    ).first()
    if existing is not None:
        return existing
    row = BankLabelSeparation(
        user_uuid_bidx=user_bidx,
        separation_bidx=bidx,
        account_bidx=hash_index(account_id, master_key),
        account_ref_enc=encrypt_data(account_id, master_key),
        credit_enc=encrypt_data(_flag(is_credit), master_key),
        signature_enc=encrypt_data(signature, master_key),
        created_at=datetime.now(timezone.utc),
    )
    session.add(row)
    return row


def delete_separation(session: Session, user_uuid: str, master_key: str, separation_id: str) -> None:
    """Regroup a label: its operations ask together again."""
    row = session.get(BankLabelSeparation, separation_id)
    if row is None or row.user_uuid_bidx != hash_index(user_uuid, master_key):
        raise SeparationNotFoundError(separation_id)
    session.delete(row)
    session.commit()


def forget_account_separations(session: Session, account_bidx: str) -> None:
    """The separations of an account, gone with it. Left to the caller to commit."""
    session.exec(sa.delete(BankLabelSeparation).where(BankLabelSeparation.account_bidx == account_bidx))


def _flag(is_credit: bool) -> str:
    return "true" if is_credit else "false"
