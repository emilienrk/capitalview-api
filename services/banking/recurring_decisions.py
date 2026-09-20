"""
What the user said of their recurring payments, as stored: one row per decision.

A decision is about a series the rebuild finds again every time, never stored
itself. So a decision keeps what it takes to find its series again: anchors,
the blind indexes of the operations the series held when decided, and an
identity — the merchant's words, the accounts, the cadence and the amount —
for when those operations come back under other ids (an account imported
again). Everything is encrypted; the anchors are blind indexes inside an
encrypted JSON, so nothing here is joinable in clear with `bank_transactions`.

Storage only: attaching decisions to series is the rebuild's
(services/banking/recurring.py).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlmodel import Session, select

from models.banking import BankRecurringSeries
from services.encryption import decrypt_data, encrypt_data, hash_index

CONFIRMED = "confirmed"
REFUSED = "refused"


class RecurringNotFoundError(LookupError):
    """No decision of this user has this id."""


@dataclass(frozen=True)
class Identity:
    """Who was paid, how and how much, when the decision was made."""
    words: tuple[str, ...]
    accounts: tuple[str, ...]
    cadence: str
    amount: Decimal
    method: str

    def to_json(self) -> dict:
        return {
            "words": list(self.words), "accounts": list(self.accounts), "cadence": self.cadence,
            "amount": str(self.amount), "method": self.method,
        }

    @classmethod
    def from_json(cls, content: dict) -> Identity:
        return cls(
            tuple(content["words"]), tuple(content["accounts"]), content["cadence"],
            Decimal(content["amount"]), content["method"],
        )


@dataclass
class Decision:
    uuid: str
    status: str
    anchors: frozenset[str]
    identity: Identity
    includes: frozenset[str] = frozenset()
    excludes: frozenset[str] = frozenset()
    name: str | None = None
    cadence: str | None = None
    ended_on: date | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def load_decisions(session: Session, user_uuid: str, master_key: str) -> list[Decision]:
    """Every decision of the user, oldest change first: they are replayed in
    that order, so the latest word on a series wins."""
    rows = session.exec(
        select(BankRecurringSeries).where(BankRecurringSeries.user_uuid_bidx == hash_index(user_uuid, master_key))
    ).all()
    decisions = [_decision(row, master_key) for row in rows]
    return sorted(decisions, key=lambda d: (d.updated_at, d.uuid))


def get_decision(session: Session, user_uuid: str, master_key: str, decision_id: str) -> tuple[BankRecurringSeries, Decision]:
    row = session.get(BankRecurringSeries, decision_id)
    if row is None or row.user_uuid_bidx != hash_index(user_uuid, master_key):
        raise RecurringNotFoundError(decision_id)
    return row, _decision(row, master_key)


def save_decision(session: Session, user_uuid: str, master_key: str, decision: Decision) -> BankRecurringSeries:
    """Write a decision, new or changed. Its update time moves to now: it is
    what tells the stored patterns that decisions moved."""
    row = session.get(BankRecurringSeries, decision.uuid)
    now = datetime.now(timezone.utc)
    if row is None:
        row = BankRecurringSeries(
            uuid=decision.uuid, user_uuid_bidx=hash_index(user_uuid, master_key), created_at=now,
            status_enc="", anchors_enc="", identity_enc="", updated_at=now,
        )
    row.status_enc = encrypt_data(decision.status, master_key)
    row.anchors_enc = encrypt_data(json.dumps(sorted(decision.anchors)), master_key)
    row.includes_enc = _encrypt_set(decision.includes, master_key)
    row.excludes_enc = _encrypt_set(decision.excludes, master_key)
    row.identity_enc = encrypt_data(json.dumps(decision.identity.to_json()), master_key)
    row.name_enc = encrypt_data(decision.name, master_key) if decision.name else None
    row.cadence_enc = encrypt_data(decision.cadence, master_key) if decision.cadence else None
    row.ended_on_enc = encrypt_data(decision.ended_on.isoformat(), master_key) if decision.ended_on else None
    row.updated_at = now
    session.add(row)
    session.commit()
    return row


def delete_decision(session: Session, user_uuid: str, master_key: str, decision_id: str) -> None:
    row, _ = get_decision(session, user_uuid, master_key, decision_id)
    session.delete(row)
    session.commit()


def forget_account(session: Session, user_bidx: str, account_uuid: str, master_key: str) -> None:
    """A bank account deleted: the decisions about it alone go with it, the
    others stop naming it. Not committed: the account's deletion commits."""
    for row in session.exec(select(BankRecurringSeries).where(BankRecurringSeries.user_uuid_bidx == user_bidx)).all():
        identity = Identity.from_json(json.loads(decrypt_data(row.identity_enc, master_key)))
        if account_uuid not in identity.accounts:
            continue
        remaining = tuple(account for account in identity.accounts if account != account_uuid)
        if not remaining:
            session.delete(row)
            continue
        kept = Identity(identity.words, remaining, identity.cadence, identity.amount, identity.method)
        row.identity_enc = encrypt_data(json.dumps(kept.to_json()), master_key)
        row.updated_at = datetime.now(timezone.utc)
        session.add(row)


def export_decisions(session: Session, user_bidx: str, master_key: str) -> list[dict]:
    """The decisions as the user can read them. The anchors are left out:
    blind indexes mean nothing outside this database."""
    rows = session.exec(
        select(BankRecurringSeries).where(BankRecurringSeries.user_uuid_bidx == user_bidx).order_by(BankRecurringSeries.created_at)
    ).all()
    exported = []
    for row in rows:
        decision = _decision(row, master_key)
        exported.append({
            "uuid": decision.uuid,
            "status": decision.status,
            "name": decision.name,
            "cadence": decision.cadence or decision.identity.cadence,
            "merchant_words": list(decision.identity.words),
            "bank_account_ids": list(decision.identity.accounts),
            "amount": str(decision.identity.amount),
            "ended_on": decision.ended_on,
            "created_at": decision.created_at,
            "updated_at": decision.updated_at,
        })
    return exported


def _decision(row: BankRecurringSeries, master_key: str) -> Decision:
    return Decision(
        uuid=row.uuid,
        status=decrypt_data(row.status_enc, master_key),
        anchors=frozenset(json.loads(decrypt_data(row.anchors_enc, master_key))),
        identity=Identity.from_json(json.loads(decrypt_data(row.identity_enc, master_key))),
        includes=_decrypt_set(row.includes_enc, master_key),
        excludes=_decrypt_set(row.excludes_enc, master_key),
        name=decrypt_data(row.name_enc, master_key) if row.name_enc else None,
        cadence=decrypt_data(row.cadence_enc, master_key) if row.cadence_enc else None,
        ended_on=date.fromisoformat(decrypt_data(row.ended_on_enc, master_key)) if row.ended_on_enc else None,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _encrypt_set(values: frozenset[str], master_key: str) -> str | None:
    return encrypt_data(json.dumps(sorted(values)), master_key) if values else None


def _decrypt_set(value: str | None, master_key: str) -> frozenset[str]:
    return frozenset(json.loads(decrypt_data(value, master_key))) if value else frozenset()
