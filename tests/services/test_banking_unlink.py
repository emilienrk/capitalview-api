"""Detaching one account, as opposed to disconnecting a whole authorization.

The re-seeding these tests pin down is not cosmetic: it is what recovers the
rows cross-account deduplication dropped while the detached account was there.
Measured on Emilien's real Boursorama export, seeding the card account before
the current one costs the current account 1 442 of its 2 776 movements and
drags its reconstructed curve from +541,81 € to -5 288,06 €.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from sqlmodel import select

from models.banking import BankAccountLink, BankSession, BankTransaction
from services.banking.linking import (
    BankAccountNotLinkedError,
    CardAccountNotLinkableError,
    link_account,
    list_session_accounts,
    unlink_account,
)
from services.banking.transactions import store_transactions
from services.encryption import hash_index
from tests.services.test_banking_sync import (
    USER,
    _account,
    _bank_session,
    _link,
    _raw,
    sqlite_pg_insert,  # noqa: F401
)

TODAY = date.today()

ACCOUNTS_PAYLOAD = [
    {"uid": "uid-cacc", "identification_hash": "h-cacc", "cash_account_type": "CACC"},
    {"uid": "uid-card", "identification_hash": "h-card", "cash_account_type": "CARD"},
]


def _pair(session, master_key):
    """A current account and a card account, both linked and both already synced."""
    bank_session = _bank_session(session, master_key, ACCOUNTS_PAYLOAD)
    current = _account(session, master_key, name="Compte courant", balance=Decimal("300"))
    card = _account(session, master_key, name="Compte plaisir", balance=Decimal("0"))
    link_current = _link(
        session, master_key, bank_session, current, "h-cacc", "uid-cacc",
        TODAY, Decimal("300"), last_synced_at=TODAY,
    )
    link_card = _link(
        session, master_key, bank_session, card, "h-card", "uid-card",
        TODAY, Decimal("0"), last_synced_at=TODAY,
    )
    return current, card, link_current, link_card


def _links_for(session, master_key, account):
    return session.exec(
        select_link(account, master_key)
    ).first()


def select_link(account, master_key):
    from sqlmodel import select

    return select(BankAccountLink).where(
        BankAccountLink.bank_account_uuid_bidx == hash_index(account.uuid, master_key)
    )


class TestUnlinkAccount:
    def test_it_detaches_only_the_account_asked_for(self, session, master_key, sqlite_pg_insert):  # noqa: F811
        current, card, _, _ = _pair(session, master_key)

        unlink_account(session, USER, master_key, card.uuid, delete_transactions=False)

        assert _links_for(session, master_key, card) is None
        # The authorization and the other attachment survive: detaching one
        # account is not disconnecting the bank.
        assert _links_for(session, master_key, current) is not None

    def test_the_accounts_own_balance_survives_the_detachment(
        self, session, master_key, sqlite_pg_insert  # noqa: F811
    ):
        """The link owned the connection, never the money."""
        _, card, _, _ = _pair(session, master_key)
        before = card.balance_enc

        unlink_account(session, USER, master_key, card.uuid, delete_transactions=True)

        session.refresh(card)
        assert card.balance_enc == before

    def test_detaching_puts_the_mirrored_account_back_into_seeding(
        self, session, master_key, sqlite_pg_insert  # noqa: F811
    ):
        """Everything the card shadowed is missing from the current account, and
        an incremental sync only reaches back to the anchor — it would never be
        fetched again. Seeding is `last_synced_at < anchor_date` (sync.py:204)."""
        current, card, link_current, _ = _pair(session, master_key)
        assert link_current.last_synced_at == link_current.anchor_date  # not seeding

        result = unlink_account(session, USER, master_key, card.uuid, delete_transactions=False)

        session.refresh(link_current)
        assert link_current.last_synced_at < link_current.anchor_date
        assert result.reseeded_accounts == [current.uuid]

    def test_two_accounts_of_the_same_kind_are_not_re_seeded(
        self, session, master_key, sqlite_pg_insert  # noqa: F811
    ):
        """Two current accounts mirror nothing, so nothing was ever deduplicated
        between them and there is nothing to recover — the pairing rule is the
        one the removed deduplication used, not "every other account"."""
        payload = [
            {"uid": "uid-a", "identification_hash": "h-a", "cash_account_type": "CACC"},
            {"uid": "uid-b", "identification_hash": "h-b", "cash_account_type": "CACC"},
        ]
        bank_session = _bank_session(session, master_key, payload)
        first = _account(session, master_key, name="Courant 1", balance=Decimal("100"))
        second = _account(session, master_key, name="Courant 2", balance=Decimal("200"))
        link_first = _link(
            session, master_key, bank_session, first, "h-a", "uid-a",
            TODAY, Decimal("100"), last_synced_at=TODAY,
        )
        _link(
            session, master_key, bank_session, second, "h-b", "uid-b",
            TODAY, Decimal("200"), last_synced_at=TODAY,
        )

        result = unlink_account(session, USER, master_key, second.uuid, delete_transactions=False)

        session.refresh(link_first)
        assert link_first.last_synced_at == link_first.anchor_date
        assert result.reseeded_accounts == []

    def test_transactions_are_deleted_only_when_asked(
        self, session, master_key, sqlite_pg_insert  # noqa: F811
    ):
        _, card, _, _ = _pair(session, master_key)
        store_transactions(
            session, USER, master_key, card.uuid,
            [_raw("12.50", TODAY, ref="a"), _raw("30", TODAY, ref="b")],
        )
        card_bidx = hash_index(card.uuid, master_key)

        def stored():
            from sqlmodel import select

            return session.exec(
                select(BankTransaction).where(BankTransaction.account_id_bidx == card_bidx)
            ).all()

        assert len(stored()) == 2

        result = unlink_account(session, USER, master_key, card.uuid, delete_transactions=False)
        assert result.transactions_deleted == 0
        assert len(stored()) == 2

    def test_deleting_the_rows_clears_what_would_shadow_a_future_attachment(
        self, session, master_key, sqlite_pg_insert  # noqa: F811
    ):
        _, card, _, _ = _pair(session, master_key)
        store_transactions(
            session, USER, master_key, card.uuid,
            [_raw("12.50", TODAY, ref="a"), _raw("30", TODAY, ref="b")],
        )

        result = unlink_account(session, USER, master_key, card.uuid, delete_transactions=True)

        assert result.transactions_deleted == 2
        card_bidx = hash_index(card.uuid, master_key)
        from sqlmodel import select

        assert session.exec(
            select(BankTransaction).where(BankTransaction.account_id_bidx == card_bidx)
        ).all() == []

    def test_an_unlinked_account_cannot_be_detached(
        self, session, master_key, sqlite_pg_insert  # noqa: F811
    ):
        manual = _account(session, master_key, name="Manuel", balance=Decimal("10"))

        with pytest.raises(BankAccountNotLinkedError):
            unlink_account(session, USER, master_key, manual.uuid, delete_transactions=False)

    def test_another_users_link_is_never_reachable(
        self, session, master_key, sqlite_pg_insert  # noqa: F811
    ):
        _, card, _, _ = _pair(session, master_key)

        with pytest.raises(BankAccountNotLinkedError):
            unlink_account(session, "someone-else", master_key, card.uuid, delete_transactions=False)


class TestCardAccountsAreNotAttachable:
    """A card account republishes the current account it debits (98 % of the real
    capture, no shared reference), and its balance is not a stock: the bank
    publishes a single OTHR, so walking a curve back from it invents money the
    account never held. It is therefore never offered, and never accepted."""

    def _session_with_both(self, session, master_key):
        return _bank_session(session, master_key, ACCOUNTS_PAYLOAD)

    def test_a_card_account_is_never_offered_for_attachment(
        self, session, master_key, sqlite_pg_insert  # noqa: F811
    ):
        self._session_with_both(session, master_key)
        bank_session = session.exec(select(BankSession)).one()

        discovered = list_session_accounts(session, USER, master_key, bank_session.uuid)

        assert [a.cash_account_type for a in discovered] == ["CACC"]

    def test_the_refusal_holds_at_the_door_and_not_only_at_the_display(
        self, session, master_key, sqlite_pg_insert  # noqa: F811
    ):
        """`list_session_accounts` hides them, but an identification_hash is
        guessable from a payload captured before the rule existed."""
        bank_session = self._session_with_both(session, master_key)
        target = _account(session, master_key, name="Compte plaisir", balance=Decimal("10"))

        with pytest.raises(CardAccountNotLinkableError):
            link_account(session, USER, master_key, bank_session.uuid, "h-card", target.uuid)

    def test_a_current_account_is_still_attachable(
        self, session, master_key, sqlite_pg_insert  # noqa: F811
    ):
        bank_session = self._session_with_both(session, master_key)
        target = _account(session, master_key, name="Compte courant", balance=Decimal("300"))

        result = link_account(session, USER, master_key, bank_session.uuid, "h-cacc", target.uuid)

        assert result.bank_account_uuid == target.uuid
        assert result.reconnected is False
