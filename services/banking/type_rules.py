"""
The cashflow type the user gave a label, and every operation it reaches.

A rule belongs to one account and one direction: "VIR INST <user>" leaving the
current account is not the same answer as the same words arriving on it. It
reaches the operations of that exact label, and those of a nearby one — a
salary whose reference changes every month would otherwise ask again each
month. Nearby is measured the way transfer decisions measure it
(`transfer_decisions.SIMILARITY_THRESHOLD`): the words both labels share over
the words either holds, once the words found in too many distinct labels on
that side of the account ("CARTE", "VIR") are set aside, since they tell
nothing apart.

Rules are applied as operations are read, never written onto them, so a rule
also types the operations imported after it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlmodel import Session, select

from dtos.banking import CashflowType
from models.banking import BankTypeRule
from services.banking.transfer_decisions import SIMILARITY_THRESHOLD
from services.encryption import decrypt_data, encrypt_data, hash_index


class RuleNotFoundError(LookupError):
    """No rule of this user has this id."""


@dataclass(frozen=True)
class TypeRule:
    uuid: str
    account_bidx: str
    is_credit: bool
    signature: str
    words: frozenset[str]
    type: CashflowType
    created_at: datetime


@dataclass
class TypeRules:
    """A user's rules, loaded once per request."""
    exact: dict[tuple[str, bool, str], TypeRule] = field(default_factory=dict)
    by_side: dict[tuple[str, bool], list[TypeRule]] = field(default_factory=dict)
    _reached: dict[tuple[str, bool, str], TypeRule | None] = field(default_factory=dict)

    def reach(
        self, account_bidx: str, is_credit: bool, signature: str | None, common: frozenset[str]
    ) -> TypeRule | None:
        """The rule of this label: its own, else the nearest one on the same
        side of the account, the most recent on a tie."""
        if signature is None:
            return None
        key = (account_bidx, is_credit, signature)
        if key in self.exact:
            return self.exact[key]
        if key not in self._reached:
            self._reached[key] = _nearest(self.by_side.get((account_bidx, is_credit), []), signature, common)
        return self._reached[key]


def _nearest(rules: list[TypeRule], signature: str, common: frozenset[str]) -> TypeRule | None:
    informative = frozenset(signature.split()) - common
    if not informative:
        return None
    best: tuple[float, datetime] | None = None
    nearest = None
    for rule in rules:
        words = rule.words - common
        if not words:
            continue
        score = len(informative & words) / len(informative | words)
        if score >= SIMILARITY_THRESHOLD and (best is None or (score, rule.created_at) > best):
            best, nearest = (score, rule.created_at), rule
    return nearest


def rule_bidx(account_id: str, is_credit: bool, signature: str, master_key: str) -> str:
    return hash_index(f"type-rule:{account_id}:{_flag(is_credit)}:{signature}", master_key)


def load_rules(session: Session, user_uuid: str, master_key: str) -> TypeRules:
    rules = TypeRules()
    for row in session.exec(
        select(BankTypeRule).where(BankTypeRule.user_uuid_bidx == hash_index(user_uuid, master_key))
    ).all():
        rule = TypeRule(
            uuid=row.uuid,
            account_bidx=hash_index(decrypt_data(row.account_ref_enc, master_key), master_key),
            is_credit=decrypt_data(row.credit_enc, master_key) == _flag(True),
            signature=decrypt_data(row.signature_enc, master_key),
            words=frozenset(json.loads(decrypt_data(row.words_enc, master_key))),
            type=CashflowType(decrypt_data(row.type_enc, master_key)),
            created_at=row.created_at,
        )
        rules.exact[(rule.account_bidx, rule.is_credit, rule.signature)] = rule
        rules.by_side.setdefault((rule.account_bidx, rule.is_credit), []).append(rule)
    return rules


def save_rule(
    session: Session,
    user_uuid: str,
    master_key: str,
    account_id: str,
    is_credit: bool,
    signature: str,
    kind: CashflowType,
) -> BankTypeRule:
    """Write the rule of a label, replacing the one it had.

    Replaced by a new row rather than updated: its creation time is what tells
    the stored transfer patterns that the rules moved.
    """
    user_bidx = hash_index(user_uuid, master_key)
    bidx = rule_bidx(account_id, is_credit, signature, master_key)
    session.exec(sa.delete(BankTypeRule).where(
        BankTypeRule.user_uuid_bidx == user_bidx, BankTypeRule.rule_bidx == bidx,
    ))
    row = BankTypeRule(
        user_uuid_bidx=user_bidx,
        rule_bidx=bidx,
        signature_enc=encrypt_data(signature, master_key),
        account_ref_enc=encrypt_data(account_id, master_key),
        credit_enc=encrypt_data(_flag(is_credit), master_key),
        words_enc=encrypt_data(json.dumps(signature.split()), master_key),
        type_enc=encrypt_data(kind.value, master_key),
        created_at=datetime.now(timezone.utc),
    )
    session.add(row)
    session.commit()
    return row


def delete_rule(session: Session, user_uuid: str, master_key: str, rule_id: str) -> None:
    row = session.get(BankTypeRule, rule_id)
    if row is None or row.user_uuid_bidx != hash_index(user_uuid, master_key):
        raise RuleNotFoundError(rule_id)
    session.delete(row)
    session.commit()


def _flag(is_credit: bool) -> str:
    return "true" if is_credit else "false"
