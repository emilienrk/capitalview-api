"""
The cashflow type the user gave a label, and every operation it reaches.

A rule belongs to one account and one direction: "VIR INST <user>" leaving the
current account is not the same answer as the same words arriving on it. It
reaches the operations of that exact label, and those of a nearby one — a
salary whose reference changes every month would otherwise ask again each
month. Nearby is measured on the words of `labels.py`, as everywhere a label
is compared (`labels.SIMILARITY_THRESHOLD`), once the words found in too many
distinct labels on that side of the account ("CARTE", "VIR") are set aside,
since they tell nothing apart.

A rule is read by its words, its key made of them as `labels.label_signature`
makes it: a rule saved before labels were read with accents folded and
references dropped whole still finds its label, and the answer given again on
that label replaces it.

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
from services.banking.labels import SIMILARITY_THRESHOLD, fold, label_signature, label_words, similarity
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
        self, account_bidx: str, is_credit: bool, label: str | None, common: frozenset[str]
    ) -> TypeRule | None:
        """The rule of this label: its own, else the nearest one on the same
        side of the account, the most recent on a tie."""
        signature = label_signature(label)
        if signature is None:
            return None
        key = (account_bidx, is_credit, signature)
        if key in self.exact:
            return self.exact[key]
        if key not in self._reached:
            self._reached[key] = _nearest(self.by_side.get((account_bidx, is_credit), []), label, common)
        return self._reached[key]


def _nearest(rules: list[TypeRule], label: str | None, common: frozenset[str]) -> TypeRule | None:
    informative = label_words(label) - common
    if not informative:
        return None
    best: tuple[float, datetime] | None = None
    nearest = None
    for rule in rules:
        words = rule.words - common
        if not words:
            continue
        score = similarity(informative, words)
        if score >= SIMILARITY_THRESHOLD and (best is None or (score, rule.created_at) > best):
            best, nearest = (score, rule.created_at), rule
    return nearest


def rule_bidx(account_id: str, is_credit: bool, signature: str, master_key: str) -> str:
    return hash_index(f"type-rule:{account_id}:{_flag(is_credit)}:{signature}", master_key)


def load_rules(session: Session, user_uuid: str, master_key: str) -> TypeRules:
    rules = TypeRules()
    for rule in _stored(session, hash_index(user_uuid, master_key), master_key):
        key = (rule.account_bidx, rule.is_credit, rule.signature)
        if key not in rules.exact or rule.created_at > rules.exact[key].created_at:
            rules.exact[key] = rule
    for rule in rules.exact.values():
        rules.by_side.setdefault((rule.account_bidx, rule.is_credit), []).append(rule)
    return rules


def _stored(session: Session, user_bidx: str, master_key: str) -> list[TypeRule]:
    rules = []
    for row in session.exec(select(BankTypeRule).where(BankTypeRule.user_uuid_bidx == user_bidx)).all():
        words = frozenset(fold(word) for word in json.loads(decrypt_data(row.words_enc, master_key)))
        rules.append(TypeRule(
            uuid=row.uuid,
            account_bidx=hash_index(decrypt_data(row.account_ref_enc, master_key), master_key),
            is_credit=decrypt_data(row.credit_enc, master_key) == _flag(True),
            signature=" ".join(sorted(words)),
            words=words,
            type=CashflowType(decrypt_data(row.type_enc, master_key)),
            created_at=row.created_at,
        ))
    return rules


def save_rule(
    session: Session,
    user_uuid: str,
    master_key: str,
    account_id: str,
    is_credit: bool,
    label: str,
    kind: CashflowType,
) -> BankTypeRule:
    """Write the rule of a label, replacing the one it had.

    Replaced by a new row rather than updated: its creation time is what tells
    the stored transfer patterns that the rules moved.
    """
    signature = label_signature(label)
    if signature is None:
        raise ValueError("A rule needs a label with words.")
    user_bidx = hash_index(user_uuid, master_key)
    bidx = rule_bidx(account_id, is_credit, signature, master_key)
    side = (hash_index(account_id, master_key), is_credit, signature)
    replaced = [
        rule.uuid for rule in _stored(session, user_bidx, master_key)
        if (rule.account_bidx, rule.is_credit, rule.signature) == side
    ]
    session.exec(sa.delete(BankTypeRule).where(
        BankTypeRule.user_uuid_bidx == user_bidx,
        sa.or_(BankTypeRule.rule_bidx == bidx, BankTypeRule.uuid.in_(replaced)),
    ))
    row = BankTypeRule(
        user_uuid_bidx=user_bidx,
        rule_bidx=bidx,
        signature_enc=encrypt_data(signature, master_key),
        account_ref_enc=encrypt_data(account_id, master_key),
        credit_enc=encrypt_data(_flag(is_credit), master_key),
        words_enc=encrypt_data(json.dumps(sorted(label_words(label))), master_key),
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
