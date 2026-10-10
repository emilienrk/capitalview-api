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

A rule can also be written by hand (docs/bank-sorting.md): every label that
contains a text, or starts with it, on one account or on all of them. Its text
is matched as typed, case, accents and runs of spaces folded, never as a
pattern. A rule given from an answer outranks one written by hand, being about
that very label; between written rules, the longest text wins, then the one
kept to an account, then the most recent.

Rules are applied as operations are read, never written onto them, so a rule
also types the operations imported after it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlmodel import Session, select

from dtos.banking import CashflowType, RuleMatch
from models.banking import BankTypeRule
from services.banking.labels import SIMILARITY_THRESHOLD, fold, label_signature, label_words, similarity
from services.encryption import decrypt_data, encrypt_data, hash_index


# Characters other than spaces. Shorter, a written rule reaches labels by
# accident ("ca" in "carte" and in "cagnotte"); longer is no rule anyone writes.
PATTERN_MIN_LENGTH, PATTERN_MAX_LENGTH = 3, 100


class RuleNotFoundError(LookupError):
    """No rule of this user has this id."""


class RulePatternError(ValueError):
    """A written rule's text, too short or too long to match on."""


@dataclass(frozen=True)
class TypeRule:
    uuid: str
    # None for a written rule over every account.
    account_bidx: str | None
    is_credit: bool
    # A written rule's folded text; the words of the label otherwise.
    signature: str
    words: frozenset[str]
    type: CashflowType
    created_at: datetime
    match: RuleMatch = RuleMatch.SIMILAR

    def matches(self, folded_label: str) -> bool:
        """Whether a written rule reaches this label, folded by `fold_text`."""
        if self.match is RuleMatch.CONTAINS:
            return self.signature in folded_label
        return self.match is RuleMatch.STARTS_WITH and folded_label.startswith(self.signature)

    def outranks(self, other: TypeRule) -> bool:
        """Between written rules: the longest text, then one kept to an account,
        then the most recent."""
        return _rank(self) > _rank(other)


def _rank(rule: TypeRule) -> tuple[int, bool, datetime]:
    created = rule.created_at
    # SQLite hands timestamps back naive; every one was written in UTC.
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return (len(rule.signature), rule.account_bidx is not None, created)


def fold_text(text: str | None) -> str:
    """A label or a rule's text as written rules compare them."""
    return " ".join(fold(text or "").split())


def written_pattern(text: str) -> str:
    folded = fold_text(text)
    if len(folded.replace(" ", "")) < PATTERN_MIN_LENGTH:
        raise RulePatternError(f"Écrivez au moins {PATTERN_MIN_LENGTH} caractères.")
    if len(folded) > PATTERN_MAX_LENGTH:
        raise RulePatternError(f"{PATTERN_MAX_LENGTH} caractères au plus.")
    return folded


@dataclass
class TypeRules:
    """A user's rules, loaded once per request."""
    exact: dict[tuple[str, bool, str], TypeRule] = field(default_factory=dict)
    by_side: dict[tuple[str, bool], list[TypeRule]] = field(default_factory=dict)
    written: list[TypeRule] = field(default_factory=list)
    _reached: dict[tuple[str, bool, str], TypeRule | None] = field(default_factory=dict)
    _written_reached: dict[tuple[str, bool, str], TypeRule | None] = field(default_factory=dict)

    def all(self) -> list[TypeRule]:
        return [*self.exact.values(), *self.written]

    def reach(
        self, account_bidx: str, is_credit: bool, label: str | None, common: frozenset[str]
    ) -> TypeRule | None:
        """The rule of this label: its own, else the nearest one on the same
        side of the account, the most recent on a tie; else a written one."""
        signature = label_signature(label)
        if signature is not None:
            key = (account_bidx, is_credit, signature)
            if key in self.exact:
                return self.exact[key]
            if key not in self._reached:
                self._reached[key] = _nearest(self.by_side.get((account_bidx, is_credit), []), label, common)
            if self._reached[key] is not None:
                return self._reached[key]
        if not self.written:
            return None
        folded = fold_text(label)
        key = (account_bidx, is_credit, folded)
        if key not in self._written_reached:
            best = None
            for rule in self.written:
                if rule.is_credit != is_credit or rule.account_bidx not in (None, account_bidx):
                    continue
                if rule.matches(folded) and (best is None or rule.outranks(best)):
                    best = rule
            self._written_reached[key] = best
        return self._written_reached[key]

    def replaced(self, rule: TypeRule, without: frozenset[str] = frozenset()) -> TypeRules:
        """These rules with one written rule added, and those it replaces gone:
        what a rule would do before it is saved."""
        rules = TypeRules()
        for kept in self.all():
            if kept.uuid not in without:
                rules._add(kept)
        rules._add(rule)
        return rules

    def same_as(self, rule: TypeRule) -> frozenset[str]:
        """The written rules saving this one replaces: the same text, match,
        account and direction."""
        return frozenset(
            other.uuid for other in self.written
            if (other.match, other.signature, other.account_bidx, other.is_credit)
            == (rule.match, rule.signature, rule.account_bidx, rule.is_credit)
        )

    def _add(self, rule: TypeRule) -> None:
        if rule.match is not RuleMatch.SIMILAR:
            self.written.append(rule)
            return
        key = (rule.account_bidx, rule.is_credit, rule.signature)
        if key in self.exact:
            if rule.created_at <= self.exact[key].created_at:
                return
            self.by_side[key[:2]].remove(self.exact[key])
        self.exact[key] = rule
        self.by_side.setdefault((rule.account_bidx, rule.is_credit), []).append(rule)


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


def written_rule_bidx(
    match: RuleMatch, account_id: str | None, is_credit: bool, pattern: str, master_key: str
) -> str:
    return hash_index(f"type-rule:{match.value}:{account_id or '*'}:{_flag(is_credit)}:{pattern}", master_key)


def load_rules(session: Session, user_uuid: str, master_key: str) -> TypeRules:
    rules = TypeRules()
    for rule in _stored(session, hash_index(user_uuid, master_key), master_key):
        rules._add(rule)
    return rules


def _stored(session: Session, user_bidx: str, master_key: str) -> list[TypeRule]:
    rules = []
    for row in session.exec(select(BankTypeRule).where(BankTypeRule.user_uuid_bidx == user_bidx)).all():
        account = decrypt_data(row.account_ref_enc, master_key) if row.account_ref_enc else None
        common = dict(
            uuid=row.uuid,
            account_bidx=hash_index(account, master_key) if account else None,
            is_credit=decrypt_data(row.credit_enc, master_key) == _flag(True),
            type=CashflowType(decrypt_data(row.type_enc, master_key)),
            created_at=row.created_at,
        )
        if row.match_enc:
            rules.append(TypeRule(
                **common, signature=decrypt_data(row.pattern_enc, master_key), words=frozenset(),
                match=RuleMatch(decrypt_data(row.match_enc, master_key)),
            ))
            continue
        words = frozenset(fold(word) for word in json.loads(decrypt_data(row.words_enc, master_key)))
        rules.append(TypeRule(**common, signature=" ".join(sorted(words)), words=words))
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
        if rule.match is RuleMatch.SIMILAR and (rule.account_bidx, rule.is_credit, rule.signature) == side
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


def save_written_rule(
    session: Session,
    user_uuid: str,
    master_key: str,
    match: RuleMatch,
    text: str,
    account_id: str | None,
    is_credit: bool,
    kind: CashflowType,
    replacing: str | None = None,
) -> BankTypeRule:
    """Write a rule by hand, in place of `replacing` when it edits one, and of
    the same rule written before."""
    if match is RuleMatch.SIMILAR:
        raise ValueError("A rule written by hand contains a text or starts with it.")
    pattern = written_pattern(text)
    if replacing is not None:
        _owned(session, user_uuid, master_key, replacing)
    user_bidx = hash_index(user_uuid, master_key)
    bidx = written_rule_bidx(match, account_id, is_credit, pattern, master_key)
    session.exec(sa.delete(BankTypeRule).where(
        BankTypeRule.user_uuid_bidx == user_bidx,
        sa.or_(BankTypeRule.rule_bidx == bidx, BankTypeRule.uuid == replacing),
    ))
    row = BankTypeRule(
        user_uuid_bidx=user_bidx,
        rule_bidx=bidx,
        signature_enc=encrypt_data(pattern, master_key),
        account_ref_enc=encrypt_data(account_id, master_key) if account_id else None,
        credit_enc=encrypt_data(_flag(is_credit), master_key),
        words_enc=encrypt_data("[]", master_key),
        type_enc=encrypt_data(kind.value, master_key),
        match_enc=encrypt_data(match.value, master_key),
        pattern_enc=encrypt_data(pattern, master_key),
        created_at=datetime.now(timezone.utc),
    )
    session.add(row)
    session.commit()
    return row


def retype_rule(session: Session, user_uuid: str, master_key: str, rule_id: str, kind: CashflowType) -> BankTypeRule:
    """Give a rule another type. Replaced by a new row, as `save_rule` does:
    its creation time is what tells the stored transfer patterns that the
    rules moved."""
    row = _owned(session, user_uuid, master_key, rule_id)
    retyped = BankTypeRule(
        user_uuid_bidx=row.user_uuid_bidx,
        rule_bidx=row.rule_bidx,
        signature_enc=row.signature_enc,
        account_ref_enc=row.account_ref_enc,
        credit_enc=row.credit_enc,
        words_enc=row.words_enc,
        type_enc=encrypt_data(kind.value, master_key),
        match_enc=row.match_enc,
        pattern_enc=row.pattern_enc,
        created_at=datetime.now(timezone.utc),
    )
    session.delete(row)
    # Before the insert: both rows share the rule's blind index, unique per user.
    session.flush()
    session.add(retyped)
    session.commit()
    return retyped


def delete_rule(session: Session, user_uuid: str, master_key: str, rule_id: str) -> None:
    session.delete(_owned(session, user_uuid, master_key, rule_id))
    session.commit()


def _owned(session: Session, user_uuid: str, master_key: str, rule_id: str) -> BankTypeRule:
    row = session.get(BankTypeRule, rule_id)
    if row is None or row.user_uuid_bidx != hash_index(user_uuid, master_key):
        raise RuleNotFoundError(rule_id)
    return row


def _flag(is_credit: bool) -> str:
    return "true" if is_credit else "false"
