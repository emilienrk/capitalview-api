"""
Declared cashflows against what actually moved (services/banking/matching.py).

Transactions go in through `store_transactions`, so the comparison reads exactly
what a real sync would have written.
"""
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import patch

from sqlmodel import Session, select

from dtos.bank import BankAccountCreate
from dtos.cashflow import CashflowCreate
from models.banking import BankAccountLink, BankSession
from models.cashflow import Cashflow
from models.enums import BankAccountType, FlowType, Frequency
from services.bank import create_bank_account
from services.banking.matching import (
    DRIFTED,
    DUPLICATED,
    MISSING,
    ON_TRACK,
    UNMATCHED,
    compare_cashflows,
    label_signature,
    set_match_pattern,
)
from services.banking.transactions import store_transactions
from services.cashflow import create_cashflow
from services.encryption import decrypt_data, encrypt_data, hash_index

USER = "match_user"
ACCOUNT = "match-account"
CARD_ACCOUNT = "match-card-account"
TODAY = date(2026, 8, 20)


def _link(session: Session, master_key: str) -> None:
    session.add(
        BankSession(
            uuid="sess-match",
            user_uuid_bidx=hash_index(USER, master_key),
            session_id_enc=encrypt_data("eb", master_key),
            status="AUTHORIZED",
            consent_valid_until=datetime(2027, 1, 1, tzinfo=timezone.utc),
            authorized_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            accounts_enc=encrypt_data(
                json.dumps([{"identification_hash": "ih", "cash_account_type": "CACC"}]),
                master_key,
            ),
        )
    )
    session.commit()
    session.add(
        BankAccountLink(
            user_uuid_bidx=hash_index(USER, master_key),
            bank_account_uuid_bidx=hash_index(ACCOUNT, master_key),
            session_uuid="sess-match",
            identification_hash_bidx=hash_index("ih", master_key),
            account_uid_enc=encrypt_data("uid", master_key),
            anchor_date=date(2026, 1, 1),
            anchor_balance_enc=encrypt_data("0", master_key),
            last_synced_at=TODAY,
        )
    )
    session.commit()


def _link_card(session: Session, master_key: str) -> None:
    """The card account that debits `ACCOUNT`, as a pre-R21 link left behind."""
    bank_session = session.get(BankSession, "sess-match")
    accounts = json.loads(decrypt_data(bank_session.accounts_enc, master_key))
    accounts.append({"identification_hash": "ih-card", "cash_account_type": "CARD"})
    bank_session.accounts_enc = encrypt_data(json.dumps(accounts), master_key)
    session.add(bank_session)
    session.add(
        BankAccountLink(
            user_uuid_bidx=hash_index(USER, master_key),
            bank_account_uuid_bidx=hash_index(CARD_ACCOUNT, master_key),
            session_uuid="sess-match",
            identification_hash_bidx=hash_index("ih-card", master_key),
            account_uid_enc=encrypt_data("uid", master_key),
            anchor_date=date(2026, 1, 1),
            anchor_balance_enc=encrypt_data("0", master_key),
            last_synced_at=TODAY,
        )
    )
    session.commit()


def _movement(day: str, amount: str, label: str, ref: str, direction: str = "DBIT",
              currency: str = "EUR") -> dict:
    return {
        "entry_reference": ref,
        "transaction_amount": {"currency": currency, "amount": amount},
        "credit_debit_indicator": direction,
        "status": "BOOK",
        "booking_date": day,
        "value_date": day,
        "transaction_date": day,
        "remittance_information": [label],
    }


def _declare(session, master_key, name, amount, flow_type=FlowType.OUTFLOW,
             frequency=Frequency.MONTHLY, bank_account_id: str | None = None) -> str:
    created = create_cashflow(
        session,
        CashflowCreate(
            name=name, flow_type=flow_type, category="Logement",
            amount=Decimal(amount), frequency=frequency,
            transaction_date=date(2026, 1, 5),
            bank_account_id=bank_account_id,
        ),
        USER, master_key,
    )
    return created.id


def _confirm(session, master_key, cashflow_id: str, pattern: str) -> None:
    cashflow = session.get(Cashflow, cashflow_id)
    set_match_pattern(cashflow, pattern, master_key)
    session.add(cashflow)
    session.commit()


def _only(session, master_key, months=6):
    [result] = compare_cashflows(session, USER, master_key, months=months, today=TODAY)
    return result


class TestSignature:
    def test_the_card_suffix_does_not_split_a_merchant(self):
        """The current account appends CB*0837, the card account does not: the
        same purchase must not land in two groups."""
        assert label_signature("CARTE 20/02/26 ATMB CB*0837") == label_signature(
            "CARTE 18/06/24 ATMB"
        )

    def test_references_and_dates_are_dropped(self):
        assert label_signature("CARTE 03/08/25 AIRBNB * HMFYWK533K") == "CARTE AIRBNB"

    def test_accents_and_case_are_folded(self):
        assert label_signature("prlv sepa Électricité") == label_signature(
            "PRLV SEPA ELECTRICITE"
        )

    def test_an_empty_label_has_no_signature(self):
        assert label_signature(None) == ""
        assert label_signature("   ") == ""


class TestVerdicts:
    def test_a_declaration_starts_unmatched_with_a_suggestion(
        self, session: Session, master_key: str
    ):
        """Nothing is linked behind the user's back: the app proposes, and says
        so, until someone confirms."""
        _link(session, master_key)
        for i, day in enumerate(("2026-06-05", "2026-07-05", "2026-08-05")):
            store_transactions(session, master_key, ACCOUNT,
                               [_movement(day, "850.00", "PRLV SEPA FONCIA", f"r{i}")])
        cashflow_id = _declare(session, master_key, "Loyer", "850.00")

        result = _only(session, master_key)
        assert result.status == UNMATCHED
        assert result.match_pattern is None
        assert result.candidates[0].pattern == "PRLV SEPA FONCIA"
        assert result.candidates[0].observed_amount == Decimal("850.00")
        assert result.candidates[0].occurrences == 3

    def test_a_confirmed_match_that_holds_is_on_track(self, session: Session, master_key: str):
        _link(session, master_key)
        for i, day in enumerate(("2026-06-05", "2026-07-05", "2026-08-05")):
            store_transactions(session, master_key, ACCOUNT,
                               [_movement(day, "850.00", "PRLV SEPA FONCIA", f"r{i}")])
        cashflow_id = _declare(session, master_key, "Loyer", "850.00")
        _confirm(session, master_key, cashflow_id, "PRLV SEPA FONCIA")

        result = _only(session, master_key)
        assert result.status == ON_TRACK
        assert result.observed_amount == Decimal("850.00")
        assert result.occurrences == 3
        assert result.last_seen == date(2026, 8, 5)
        assert [r.amount for r in result.recent] == [Decimal("850.00")] * 3

    def test_a_rent_that_went_up_reads_as_drifted(self, session: Session, master_key: str):
        """The whole point: the declared 850 is stale and nothing else says so."""
        _link(session, master_key)
        for i, day in enumerate(("2026-06-05", "2026-07-05", "2026-08-05")):
            store_transactions(session, master_key, ACCOUNT,
                               [_movement(day, "880.00", "PRLV SEPA FONCIA", f"r{i}")])
        cashflow_id = _declare(session, master_key, "Loyer", "850.00")
        _confirm(session, master_key, cashflow_id, "PRLV SEPA FONCIA")

        result = _only(session, master_key)
        assert result.status == DRIFTED
        assert result.observed_amount == Decimal("880.00")

    def test_a_few_cents_of_movement_is_not_a_drift(self, session: Session, master_key: str):
        _link(session, master_key)
        for i, (day, amount) in enumerate(
            (("2026-06-05", "12.49"), ("2026-07-05", "12.50"), ("2026-08-05", "12.51"))
        ):
            store_transactions(session, master_key, ACCOUNT,
                               [_movement(day, amount, "PRLV SEPA SPOTIFY", f"r{i}")])
        cashflow_id = _declare(session, master_key, "Musique", "12.50")
        _confirm(session, master_key, cashflow_id, "PRLV SEPA SPOTIFY")

        assert _only(session, master_key).status == ON_TRACK

    def test_the_drift_threshold_sits_where_it_is_documented(
        self, session: Session, master_key: str
    ):
        """2 % of the declared amount, floored at 1 €. Pinned so the next person
        to widen it has to say so out loud."""
        _link(session, master_key)
        # 3 % above 900 — over the line.
        for i, day in enumerate(("2026-06-05", "2026-07-05", "2026-08-05")):
            store_transactions(session, master_key, ACCOUNT,
                               [_movement(day, "927.00", "PRLV SEPA FONCIA", f"r{i}")])
        cashflow_id = _declare(session, master_key, "Loyer", "900.00")
        _confirm(session, master_key, cashflow_id, "PRLV SEPA FONCIA")
        assert _only(session, master_key).status == DRIFTED

    def test_a_subscription_that_stopped_reads_as_missing(
        self, session: Session, master_key: str
    ):
        """Declared, confirmed, and gone quiet for months — the one you keep
        budgeting for and no longer pay."""
        _link(session, master_key)
        for i, day in enumerate(("2026-03-05", "2026-04-05")):
            store_transactions(session, master_key, ACCOUNT,
                               [_movement(day, "9.99", "PRLV SEPA GYMCLUB", f"r{i}")])
        cashflow_id = _declare(session, master_key, "Salle de sport", "9.99")
        _confirm(session, master_key, cashflow_id, "PRLV SEPA GYMCLUB")

        result = _only(session, master_key)
        assert result.status == MISSING
        assert result.last_seen == date(2026, 4, 5)

    def test_a_confirmed_match_with_nothing_behind_it_is_missing(
        self, session: Session, master_key: str
    ):
        _link(session, master_key)
        cashflow_id = _declare(session, master_key, "Loyer", "850.00")
        _confirm(session, master_key, cashflow_id, "PRLV SEPA INTROUVABLE")

        result = _only(session, master_key)
        assert result.status == MISSING
        assert result.occurrences == 0

    def test_twice_in_one_month_on_a_monthly_flow_is_reported(
        self, session: Session, master_key: str
    ):
        """Never silently smoothed: a double debit is something to take up with
        the bank, and averaging it away would hide it for good."""
        _link(session, master_key)
        for i, day in enumerate(("2026-07-05", "2026-08-05", "2026-08-14")):
            store_transactions(session, master_key, ACCOUNT,
                               [_movement(day, "58.55", "PRLV SEPA EDF", f"r{i}")])
        cashflow_id = _declare(session, master_key, "Électricité", "58.55")
        _confirm(session, master_key, cashflow_id, "PRLV SEPA EDF")

        assert _only(session, master_key).status == DUPLICATED

    def test_direction_is_respected(self, session: Session, master_key: str):
        """A credit can never satisfy a declared expense, whatever its label."""
        _link(session, master_key)
        for i, day in enumerate(("2026-07-05", "2026-08-05")):
            store_transactions(session, master_key, ACCOUNT,
                               [_movement(day, "850.00", "PRLV SEPA FONCIA", f"r{i}",
                                          direction="CRDT")])
        cashflow_id = _declare(session, master_key, "Loyer", "850.00")
        _confirm(session, master_key, cashflow_id, "PRLV SEPA FONCIA")

        assert _only(session, master_key).status == MISSING

    def test_an_inactive_declaration_is_left_out(self, session: Session, master_key: str):
        _link(session, master_key)
        cashflow_id = _declare(session, master_key, "Loyer", "850.00")
        cashflow = session.get(Cashflow, cashflow_id)
        cashflow.is_active_enc = encrypt_data("false", master_key)
        session.add(cashflow)
        session.commit()

        assert compare_cashflows(session, USER, master_key, today=TODAY) == []


class TestSuggestion:
    def test_a_single_occurrence_is_never_proposed(self, session: Session, master_key: str):
        """One movement is an event, not a recurrence."""
        _link(session, master_key)
        store_transactions(session, master_key, ACCOUNT,
                           [_movement("2026-08-05", "850.00", "PRLV SEPA FONCIA", "r0")])
        _declare(session, master_key, "Loyer", "850.00")

        assert _only(session, master_key).candidates == []

    def test_regularity_breaks_a_tie_between_equal_amounts(
        self, session: Session, master_key: str
    ):
        """Two groups at the same amount. One covers the window at the declared
        monthly cadence, the other is three purchases in a single week — which is
        what a coincidence looks like."""
        _link(session, master_key)
        for i, day in enumerate(("2026-06-05", "2026-07-05", "2026-08-05")):
            store_transactions(session, master_key, ACCOUNT,
                               [_movement(day, "30.00", "PRLV SEPA MONTHLY", f"m{i}")])
        for i, day in enumerate(("2026-08-01", "2026-08-03", "2026-08-05")):
            store_transactions(session, master_key, ACCOUNT,
                               [_movement(day, "30.00", "CARTE DAILY SHOP", f"d{i}")])
        _declare(session, master_key, "Abonnement", "30.00")

        assert _only(session, master_key).candidates[0].pattern == "PRLV SEPA MONTHLY"

    def test_a_regular_group_beats_a_closer_but_rarer_amount(
        self, session: Session, master_key: str
    ):
        """A group seen twice whose two dates happen to sit a month apart looks
        perfectly monthly and means nothing. Real data made this exact mistake:
        a card purchase at 58,65 € outranked the 60,00 € electricity bill."""
        _link(session, master_key)
        for i, day in enumerate(
            ("2026-03-05", "2026-04-05", "2026-05-05", "2026-06-05", "2026-07-05")
        ):
            store_transactions(session, master_key, ACCOUNT,
                               [_movement(day, "60.00", "PRLV SEPA EDF", f"e{i}")])
        for i, day in enumerate(("2026-06-11", "2026-07-11")):
            store_transactions(session, master_key, ACCOUNT,
                               [_movement(day, "58.65", "CARTE LECLERC", f"l{i}")])
        _declare(session, master_key, "Électricité", "58.55")

        candidates = _only(session, master_key).candidates
        assert candidates[0].pattern == "PRLV SEPA EDF"
        # The other one is still offered: only the user can settle it.
        assert "CARTE LECLERC" in [c.pattern for c in candidates]

    def test_at_most_five_candidates_are_offered(self, session: Session, master_key: str):
        _link(session, master_key)
        for n in range(8):
            for i, day in enumerate(("2026-06-05", "2026-07-05")):
                store_transactions(session, master_key, ACCOUNT,
                                   [_movement(day, f"{50 + n}.00", f"CARTE SHOP{chr(65+n)}", f"s{n}{i}")])
        _declare(session, master_key, "Divers", "55.00")

        assert len(_only(session, master_key).candidates) == 5

    def test_clearing_the_pattern_goes_back_to_suggesting(
        self, session: Session, master_key: str
    ):
        _link(session, master_key)
        for i, day in enumerate(("2026-07-05", "2026-08-05")):
            store_transactions(session, master_key, ACCOUNT,
                               [_movement(day, "850.00", "PRLV SEPA FONCIA", f"r{i}")])
        cashflow_id = _declare(session, master_key, "Loyer", "850.00")
        _confirm(session, master_key, cashflow_id, "PRLV SEPA FONCIA")
        assert _only(session, master_key).status == ON_TRACK

        cashflow = session.get(Cashflow, cashflow_id)
        set_match_pattern(cashflow, "", master_key)
        session.add(cashflow)
        session.commit()

        result = _only(session, master_key)
        assert result.status == UNMATCHED
        assert result.match_pattern is None


class TestMirroredCardAccount:
    """The card account republishes the current account's rows (R22 keeps both).

    Read unfiltered, one movement becomes two occurrences of the same signature
    — enough to turn a single purchase into a recurrence, and a monthly flow
    into a `duplicated` verdict on a perfectly healthy declaration.
    """

    def _both_sides(self, session: Session, master_key: str, day: str, ref: str) -> None:
        for account in (ACCOUNT, CARD_ACCOUNT):
            store_transactions(session, master_key, account,
                               [_movement(day, "9.99", "PRLV SEPA GYMCLUB", f"{ref}-{account}")])

    def test_a_healthy_monthly_flow_is_not_reported_as_duplicated(
        self, session: Session, master_key: str
    ):
        _link(session, master_key)
        _link_card(session, master_key)
        for i, day in enumerate(("2026-06-05", "2026-07-05", "2026-08-05")):
            self._both_sides(session, master_key, day, f"r{i}")
        cashflow_id = _declare(session, master_key, "Salle de sport", "9.99")
        _confirm(session, master_key, cashflow_id, "PRLV SEPA GYMCLUB")

        result = _only(session, master_key)
        assert result.status == ON_TRACK
        assert result.occurrences == 3

    def test_one_purchase_seen_twice_is_not_a_recurrence(
        self, session: Session, master_key: str
    ):
        """Below MIN_OCCURRENCES it must stay: the echo is not a second event."""
        _link(session, master_key)
        _link_card(session, master_key)
        self._both_sides(session, master_key, "2026-08-05", "r0")
        _declare(session, master_key, "Salle de sport", "9.99")

        assert _only(session, master_key).candidates == []


class TestCurrency:
    """A declaration is denominated by the account it hits; observed amounts
    arrive unconverted, with no rate attached. Comparing across the two would
    read `1000` against `1000` and call it a match."""

    def _chf_account(self, session: Session, master_key: str) -> str:
        with patch("services.bank.has_exchange_rate", return_value=True):
            return create_bank_account(
                session,
                BankAccountCreate(name="Compte CHF", balance=Decimal("0"),
                                  account_type=BankAccountType.CHECKING, currency="CHF"),
                USER, master_key,
            ).id

    def _declare_in_chf(self, session: Session, master_key: str) -> str:
        return _declare(session, master_key, "Loyer", "1000.00",
                        bank_account_id=self._chf_account(session, master_key))

    def test_a_euro_movement_never_answers_a_franc_declaration(
        self, session: Session, master_key: str
    ):
        _link(session, master_key)
        for i, day in enumerate(("2026-06-05", "2026-07-05", "2026-08-05")):
            store_transactions(session, master_key, ACCOUNT,
                               [_movement(day, "1000.00", "PRLV SEPA FONCIA", f"r{i}")])
        cashflow_id = self._declare_in_chf(session, master_key)
        _confirm(session, master_key, cashflow_id, "PRLV SEPA FONCIA")

        result = _only(session, master_key)
        assert result.currency == "CHF"
        # It fails rather than inventing: nothing moved in francs.
        assert result.status == MISSING

    def test_a_euro_movement_is_not_even_suggested_for_it(
        self, session: Session, master_key: str
    ):
        _link(session, master_key)
        for i, day in enumerate(("2026-06-05", "2026-07-05", "2026-08-05")):
            store_transactions(session, master_key, ACCOUNT,
                               [_movement(day, "1000.00", "PRLV SEPA FONCIA", f"r{i}")])
        self._declare_in_chf(session, master_key)

        assert _only(session, master_key).candidates == []

    def test_a_franc_movement_matches_a_franc_declaration(
        self, session: Session, master_key: str
    ):
        _link(session, master_key)
        for i, day in enumerate(("2026-06-05", "2026-07-05", "2026-08-05")):
            store_transactions(session, master_key, ACCOUNT,
                               [_movement(day, "1000.00", "PRLV SEPA FONCIA", f"r{i}",
                                          currency="CHF")])
        cashflow_id = self._declare_in_chf(session, master_key)
        _confirm(session, master_key, cashflow_id, "PRLV SEPA FONCIA")

        result = _only(session, master_key)
        assert result.status == ON_TRACK
        assert result.observed_amount == Decimal("1000.00")

    def test_an_unattached_declaration_is_compared_in_euros(
        self, session: Session, master_key: str
    ):
        """No account to be denominated by means euros, like every aggregate."""
        _link(session, master_key)
        for i, day in enumerate(("2026-06-05", "2026-07-05", "2026-08-05")):
            store_transactions(session, master_key, ACCOUNT,
                               [_movement(day, "1000.00", "PRLV SEPA FONCIA", f"r{i}")])
        cashflow_id = _declare(session, master_key, "Loyer", "1000.00")
        _confirm(session, master_key, cashflow_id, "PRLV SEPA FONCIA")

        result = _only(session, master_key)
        assert result.currency == "EUR"
        assert result.status == ON_TRACK
