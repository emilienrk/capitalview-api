"""
What the user said of an operation facing a deposit or a withdrawal they
declared on an investment account: it is that movement, or it is not.

A same-day amount is only a coincidence until the user says so — a 200 € card
payment the day of a 200 € deposit looks the same (services/banking/contributions.py).
So the question is asked, and the answer kept on both sides, as a pair decision
keeps its two legs:

- **yes** binds the operation to that declared movement, whatever else matches
  that day, and counts it as an investment — out of any pair but one the user
  binds, which withdraws the yes, as a type given by hand does. It also vouches
  for its label: the next operation of that label, from that account, facing
  on the very day a movement of that same investment account, is that movement
  again. That lesson
  is drawn from the whole history once, with the transfer patterns
  (`flows.transfer_patterns`), so every page reads an operation the same way;
- **no** takes the operation out of the matching: it reads as any other, and
  the declared movement is free to face another one.

A decision holds while its two sides still face each other (`contributions.fits`):
a deposit deleted or moved since leaves the operation read as if nothing had
been said. A rule teaches nothing here: it speaks for a label, and one label may
go to the investment account one day and anywhere the next.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlmodel import Session, select

from dtos.banking import BankContributionDecisionKind
from models.banking import BankContributionDecision
from models.currency import BASE_CURRENCY
from services.banking.contributions import Candidate, Contribution, fits, load_contributions
from services.banking.linking import readable_account_bidxs
from services.banking.transactions import row_date
from services.banking.transfer_decisions import DecisionError, _readable_leg
from services.encryption import decrypt_data, encrypt_data, hash_index


class ContributionNotFoundError(LookupError):
    """No deposit or withdrawal of this user's has this id."""


@dataclass
class ContributionDecisions:
    # Operation ref bidx -> the declared movement's ref bidx, by answer.
    confirmed: dict[str, str] = field(default_factory=dict)
    refused: dict[str, str] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.confirmed or self.refused)


def load_contribution_decisions(session: Session, user_uuid: str, master_key: str) -> ContributionDecisions:
    decisions = ContributionDecisions()
    for row in session.exec(
        select(BankContributionDecision).where(
            BankContributionDecision.user_uuid_bidx == hash_index(user_uuid, master_key)
        )
    ).all():
        kind = BankContributionDecisionKind(decrypt_data(row.kind_enc, master_key))
        answers = decisions.confirmed if kind is BankContributionDecisionKind.CONTRIBUTION else decisions.refused
        answers[row.transaction_ref_bidx] = row.contribution_ref_bidx
    return decisions


def record_contribution_decision(
    session: Session,
    user_uuid: str,
    master_key: str,
    transaction_id: str,
    contribution_id: str,
    kind: BankContributionDecisionKind,
) -> None:
    """Say this operation is, or is not, that declared movement, replacing what
    was said of it before.

    A yes also drops the yes another operation held on the same movement: a
    deposit stands for one operation at most. And the type forced on the
    operation, if any: what it is decides how it counts.
    """
    user_bidx = hash_index(user_uuid, master_key)
    readable = set(readable_account_bidxs(session, user_bidx, master_key))
    leg = _readable_leg(session, transaction_id, readable, master_key)
    contribution = _contribution(session, user_uuid, master_key, contribution_id)
    day = row_date(leg.row, master_key)
    if (
        day is None
        or leg.currency != BASE_CURRENCY
        or not fits(Candidate(0, day, leg.amount, leg.is_credit), contribution)
    ):
        raise DecisionError("Ce mouvement ne fait pas face à cette opération.")

    transaction_ref = hash_index(leg.row.uuid, master_key)
    contribution_ref = hash_index(contribution.ref, master_key)
    same_user = BankContributionDecision.user_uuid_bidx == user_bidx
    session.exec(sa.delete(BankContributionDecision).where(
        same_user, BankContributionDecision.transaction_ref_bidx == transaction_ref,
    ))
    if kind is BankContributionDecisionKind.CONTRIBUTION:
        for other in session.exec(
            select(BankContributionDecision).where(
                same_user, BankContributionDecision.contribution_ref_bidx == contribution_ref,
            )
        ).all():
            if BankContributionDecisionKind(decrypt_data(other.kind_enc, master_key)) is kind:
                session.delete(other)
        if leg.row.type_override_enc is not None:
            leg.row.type_override_enc = None
            leg.row.type_override_at = None
            session.add(leg.row)

    session.add(
        BankContributionDecision(
            user_uuid_bidx=user_bidx,
            kind_enc=encrypt_data(kind.value, master_key),
            transaction_ref_bidx=transaction_ref,
            account_bidx=leg.account_bidx,
            contribution_ref_bidx=contribution_ref,
            created_at=datetime.now(timezone.utc),
        )
    )
    session.commit()


def drop_yes(session: Session, user_bidx: str, transaction_refs: list[str], master_key: str) -> None:
    """Withdraw the yes these operations held, uncommitted: binding one into a
    pair or typing it by hand says what it is instead. A no stays, being no
    claim about what the operation is."""
    for row in session.exec(
        select(BankContributionDecision).where(
            BankContributionDecision.user_uuid_bidx == user_bidx,
            BankContributionDecision.transaction_ref_bidx.in_(transaction_refs),  # type: ignore[attr-defined]
        )
    ).all():
        if BankContributionDecisionKind(decrypt_data(row.kind_enc, master_key)) is BankContributionDecisionKind.CONTRIBUTION:
            session.delete(row)


def forget_contribution_decision(session: Session, user_uuid: str, master_key: str, decision_id: str) -> bool:
    """Withdraw one decision; False when the user holds none under this id."""
    deleted = session.exec(
        sa.delete(BankContributionDecision).where(
            BankContributionDecision.uuid == decision_id,
            BankContributionDecision.user_uuid_bidx == hash_index(user_uuid, master_key),
        )
    )
    session.commit()
    return bool(deleted.rowcount)


def _contribution(session: Session, user_uuid: str, master_key: str, contribution_id: str) -> Contribution:
    for contribution in load_contributions(session, user_uuid, master_key).all():
        if contribution.ref == contribution_id:
            return contribution
    raise ContributionNotFoundError(contribution_id)
