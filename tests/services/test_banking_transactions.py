"""
Tests for bank transaction storage and the three-level deduplication (spec §E/§F).

Two of the three levels can only be exercised with the *shapes* the bank really
returns, so the cross-account cases replay the captured Boursorama payloads
(vendor-docs/spike/*.json). Those files live outside the git repository, hence
the skip guard — never a failure.
"""
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from models.banking import BankAccountLink, BankSession, BankTransaction
from services.banking.transactions import normalize_transaction, store_transactions
from services.encryption import decrypt_data, encrypt_data, hash_index

SPIKE_DIR = Path(__file__).resolve().parents[3] / "vendor-docs" / "spike"

USER = "user-1"
CURRENT_ACCOUNT = "bank-account-current"
CARD_ACCOUNT = "bank-account-card"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _raw(**overrides) -> dict:
    """A transaction shaped exactly like the captured Boursorama payloads."""
    raw = {
        "entry_reference": "31367608838",
        "merchant_category_code": None,
        "transaction_amount": {"currency": "EUR", "amount": "10.28"},
        "creditor": None,
        "creditor_account": None,
        "credit_debit_indicator": "DBIT",
        "status": "BOOK",
        "booking_date": "2026-08-17",
        "value_date": "2026-08-16",
        "transaction_date": "2026-08-16",
        "balance_after_transaction": None,
        "reference_number": None,
        "remittance_information": ["VIR INST HUGO ODY", "Virement de Emilien ROUKINE"],
        "exchange_rate": None,
        "note": None,
        "transaction_id": None,
    }
    raw.update(overrides)
    return raw


@pytest.fixture
def linked_accounts(session: Session, master_key: str) -> None:
    """The user's two linked accounts — level 3 is scoped to the user through these."""
    bank_session = BankSession(
        user_uuid_bidx=hash_index(USER, master_key),
        session_id_enc=encrypt_data("eb-session-id", master_key),
        status="AUTHORIZED",
        consent_valid_until=datetime(2026, 12, 1, tzinfo=timezone.utc),
        authorized_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    session.add(bank_session)
    session.commit()

    for account_uuid, ident in ((CURRENT_ACCOUNT, "ident-current"), (CARD_ACCOUNT, "ident-card")):
        session.add(
            BankAccountLink(
                user_uuid_bidx=hash_index(USER, master_key),
                bank_account_uuid_bidx=hash_index(account_uuid, master_key),
                session_uuid=bank_session.uuid,
                identification_hash_bidx=hash_index(ident, master_key),
                account_uid_enc=encrypt_data(f"uid-{ident}", master_key),
                anchor_date=date(2026, 8, 1),
                anchor_balance_enc=encrypt_data("1000.00", master_key),
                last_synced_at=date(2026, 8, 1),
            )
        )
    session.commit()


def _rows(session: Session, master_key: str, account_uuid: str) -> list[BankTransaction]:
    return list(
        session.exec(
            select(BankTransaction).where(
                BankTransaction.account_id_bidx == hash_index(account_uuid, master_key)
            )
        ).all()
    )


def _load_spike(name: str) -> list[dict]:
    path = SPIKE_DIR / name
    if not path.exists():
        pytest.skip(f"captured payload {path} not available (lives outside the repo)")
    return json.loads(path.read_text())


# ---------------------------------------------------------------------------
# BankTransaction — the composite key makes level 1 unfalsifiable in the DB
# ---------------------------------------------------------------------------


def _row(master_key: str, account_uuid: str, entry_reference: str | None) -> BankTransaction:
    return BankTransaction(
        account_id_bidx=hash_index(account_uuid, master_key),
        period_bidx=hash_index("2026-08", master_key),
        entry_ref_bidx=hash_index(entry_reference, master_key) if entry_reference else None,
        dedup_bidx=hash_index("2026-08-17|10.28|EUR|DBIT", master_key),
        amount_enc=encrypt_data("10.28", master_key),
        currency_enc=encrypt_data("EUR", master_key),
        credit_debit_enc=encrypt_data("DBIT", master_key),
        status_enc=encrypt_data("BOOK", master_key),
    )


def test_same_reference_twice_on_one_account_is_rejected_by_the_database(
    session: Session, master_key: str
):
    session.add(_row(master_key, CURRENT_ACCOUNT, "31367608838"))
    session.commit()

    session.add(_row(master_key, CURRENT_ACCOUNT, "31367608838"))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_rows_without_a_reference_escape_the_unique_index(session: Session, master_key: str):
    session.add(_row(master_key, CURRENT_ACCOUNT, None))
    session.add(_row(master_key, CURRENT_ACCOUNT, None))
    session.commit()

    assert len(_rows(session, master_key, CURRENT_ACCOUNT)) == 2


# ---------------------------------------------------------------------------
# normalize_transaction — §F
# ---------------------------------------------------------------------------


def test_normalize_prefers_booking_date():
    tx = normalize_transaction(_raw())
    assert tx.effective_date == date(2026, 8, 17)


def test_normalize_falls_back_to_transaction_date_when_booking_date_is_absent():
    # 2 of the 297 real current-account transactions have no booking_date.
    tx = normalize_transaction(_raw(booking_date=None, value_date=None, transaction_date="2026-08-15"))
    assert tx.effective_date == date(2026, 8, 15)
    assert tx.booking_date is None


def test_normalize_falls_back_to_value_date_last():
    # transaction_date is systematically null in the export but populated in
    # direct API calls: field availability varies with the access path.
    tx = normalize_transaction(_raw(booking_date=None, transaction_date=None, value_date="2026-08-14"))
    assert tx.effective_date == date(2026, 8, 14)


def test_normalize_returns_no_effective_date_when_all_three_are_absent():
    tx = normalize_transaction(_raw(booking_date=None, transaction_date=None, value_date=None))
    assert tx.effective_date is None


def test_normalize_keeps_amounts_as_exact_decimals():
    tx = normalize_transaction(_raw(transaction_amount={"currency": "EUR", "amount": "0.07"}))
    assert isinstance(tx.amount, Decimal)
    assert tx.amount == Decimal("0.07")
    assert tx.amount * 3 == Decimal("0.21")


def test_normalize_joins_multiline_remittance_information():
    tx = normalize_transaction(_raw(remittance_information=["CARTE 11/08/26", " INTER GAOLETTE ", ""]))
    assert tx.remittance == "CARTE 11/08/26 INTER GAOLETTE"


def test_normalize_returns_no_remittance_when_absent():
    assert normalize_transaction(_raw(remittance_information=None)).remittance is None


def test_normalize_marks_foreign_currency_as_unconverted():
    # A real CHF operation arrives with exchange_rate = null: no rate, no conversion.
    tx = normalize_transaction(
        _raw(transaction_amount={"currency": "CHF", "amount": "12.63"}, exchange_rate=None)
    )
    assert tx.currency == "CHF"
    assert tx.amount == Decimal("12.63")
    assert tx.unconverted is True
    assert normalize_transaction(_raw()).unconverted is False


def test_normalize_accepts_an_absent_entry_reference():
    assert normalize_transaction(_raw(entry_reference=None)).entry_reference is None


def test_normalize_rejects_a_payload_missing_a_mandatory_field():
    # amount, direction and status are the contract's only required fields;
    # degrading any of them silently would poison the dedup fingerprint.
    with pytest.raises(ValueError):
        normalize_transaction(_raw(transaction_amount={"currency": "EUR", "amount": None}))
    with pytest.raises(ValueError):
        normalize_transaction(_raw(credit_debit_indicator=None))
    with pytest.raises(ValueError):
        normalize_transaction(_raw(status=None))


def test_normalize_never_uses_transaction_id():
    # transaction_id changes between calls; it must not influence identity.
    first = normalize_transaction(_raw(transaction_id="call-1-abc"))
    second = normalize_transaction(_raw(transaction_id="call-2-xyz"))
    assert first == second


# ---------------------------------------------------------------------------
# store_transactions — level 1: intra-account, by entry_reference
# ---------------------------------------------------------------------------


def test_store_inserts_new_transactions(session: Session, master_key: str, linked_accounts):
    result = store_transactions(session, USER, master_key, CURRENT_ACCOUNT, [_raw()])

    assert result == (1, 0, 0)
    (row,) = _rows(session, master_key, CURRENT_ACCOUNT)
    assert decrypt_data(row.amount_enc, master_key) == "10.28"
    assert decrypt_data(row.currency_enc, master_key) == "EUR"
    assert decrypt_data(row.credit_debit_enc, master_key) == "DBIT"
    assert decrypt_data(row.status_enc, master_key) == "BOOK"
    assert decrypt_data(row.booking_date_enc, master_key) == "2026-08-17"
    assert row.period_bidx == hash_index("2026-08", master_key)
    assert row.entry_ref_bidx == hash_index("31367608838", master_key)


def test_same_reference_on_same_account_is_deduplicated(session: Session, master_key: str, linked_accounts):
    store_transactions(session, USER, master_key, CURRENT_ACCOUNT, [_raw()])
    # A later sync returns the same operation, with a fresh transaction_id.
    result = store_transactions(session, USER, master_key, CURRENT_ACCOUNT, [_raw(transaction_id="other")])

    assert result == (0, 1, 0)
    assert len(_rows(session, master_key, CURRENT_ACCOUNT)) == 1


def test_same_reference_on_two_accounts_is_not_deduplicated(session: Session, master_key: str, linked_accounts):
    # entry_reference is explicitly not globally unique: two accounts may reuse
    # one for genuinely different operations. Only the composite key deduplicates.
    store_transactions(session, USER, master_key, CURRENT_ACCOUNT, [_raw(entry_reference="42")])
    result = store_transactions(
        session,
        USER,
        master_key,
        CARD_ACCOUNT,
        [
            _raw(
                entry_reference="42",
                transaction_amount={"currency": "EUR", "amount": "99.00"},
                booking_date="2026-07-02",
                value_date="2026-07-02",
                transaction_date="2026-07-02",
            )
        ],
    )

    assert result == (1, 0, 0)
    assert len(_rows(session, master_key, CURRENT_ACCOUNT)) == 1
    assert len(_rows(session, master_key, CARD_ACCOUNT)) == 1


# ---------------------------------------------------------------------------
# store_transactions — level 2: fallback on dedup_bidx within the account
# ---------------------------------------------------------------------------


def test_transactions_without_reference_fall_back_to_the_dedup_index(
    session: Session, master_key: str, linked_accounts
):
    batch = [
        _raw(entry_reference=None),
        _raw(entry_reference=None, transaction_amount={"currency": "EUR", "amount": "3.50"}),
    ]

    assert store_transactions(session, USER, master_key, CURRENT_ACCOUNT, batch) == (2, 0, 0)
    # Re-syncing the very same feed must correct the rows, never duplicate them.
    assert store_transactions(session, USER, master_key, CURRENT_ACCOUNT, batch) == (0, 2, 0)
    assert len(_rows(session, master_key, CURRENT_ACCOUNT)) == 2


def test_two_identical_operations_in_one_batch_are_both_kept(
    session: Session, master_key: str, linked_accounts
):
    # Same day, same amount, same direction, distinct references: two real
    # operations, not one seen twice.
    batch = [_raw(entry_reference="ref-a"), _raw(entry_reference="ref-b")]

    assert store_transactions(session, USER, master_key, CURRENT_ACCOUNT, batch) == (2, 0, 0)


def test_pending_becoming_booked_with_a_new_reference_updates_the_row(
    session: Session, master_key: str, linked_accounts
):
    # Both shapes are the ones the bank really produced: a pending operation
    # carries no booking_date at all, and a booked one carries a booking_date
    # that differs from its transaction_date (206 of the 294 booked rows of the
    # captured current account). Booking therefore moves the retained date and
    # the reference at the same time — neither level 1 nor a same-date lookup
    # can find the pending row.
    pending = _raw(
        entry_reference="avis-7575adaa5ee1609c529260924ef7f489",
        status="PDNG",
        booking_date=None,
        value_date=None,
        transaction_date="2026-08-15",
    )
    store_transactions(session, USER, master_key, CURRENT_ACCOUNT, [pending])

    booked = _raw(
        entry_reference="5852467521",
        status="BOOK",
        booking_date="2026-08-17",
        value_date="2026-08-17",
        transaction_date="2026-08-15",
    )
    result = store_transactions(session, USER, master_key, CURRENT_ACCOUNT, [booked])

    assert result == (0, 1, 0)
    (row,) = _rows(session, master_key, CURRENT_ACCOUNT)
    assert decrypt_data(row.status_enc, master_key) == "BOOK"
    assert row.entry_ref_bidx == hash_index("5852467521", master_key)
    # The row is re-keyed on its new retained date, not left on the pending one.
    assert decrypt_data(row.booking_date_enc, master_key) == "2026-08-17"
    assert row.period_bidx == hash_index("2026-08", master_key)


def test_a_booked_row_is_never_claimed_by_another_date_of_a_later_transaction(
    session: Session, master_key: str, linked_accounts
):
    # The multi-date lookup above must stay confined to claimable rows: a booked
    # row carrying its own reference is a settled, distinct operation.
    settled = _raw(entry_reference="settled-1", status="BOOK", booking_date="2026-08-15")
    store_transactions(session, USER, master_key, CURRENT_ACCOUNT, [settled])

    later = _raw(
        entry_reference="settled-2",
        status="BOOK",
        booking_date="2026-08-17",
        value_date="2026-08-17",
        transaction_date="2026-08-15",
    )
    result = store_transactions(session, USER, master_key, CURRENT_ACCOUNT, [later])

    assert result == (1, 0, 0)
    assert len(_rows(session, master_key, CURRENT_ACCOUNT)) == 2


def test_cancelled_status_invalidates_an_already_ingested_row(
    session: Session, master_key: str, linked_accounts
):
    store_transactions(session, USER, master_key, CURRENT_ACCOUNT, [_raw()])

    result = store_transactions(session, USER, master_key, CURRENT_ACCOUNT, [_raw(status="CNCL")])

    assert result == (0, 1, 0)
    assert _rows(session, master_key, CURRENT_ACCOUNT) == []


def test_rejected_status_invalidates_an_already_ingested_row(
    session: Session, master_key: str, linked_accounts
):
    store_transactions(session, USER, master_key, CURRENT_ACCOUNT, [_raw(status="PDNG")])

    result = store_transactions(session, USER, master_key, CURRENT_ACCOUNT, [_raw(status="RJCT")])

    assert result == (0, 1, 0)
    assert _rows(session, master_key, CURRENT_ACCOUNT) == []


def test_cancelled_transaction_never_ingested_is_skipped(session: Session, master_key: str, linked_accounts):
    assert store_transactions(session, USER, master_key, CURRENT_ACCOUNT, [_raw(status="CNCL")]) == (0, 0, 1)
    assert _rows(session, master_key, CURRENT_ACCOUNT) == []


def test_transaction_without_any_date_is_skipped(session: Session, master_key: str, linked_accounts):
    undated = _raw(booking_date=None, value_date=None, transaction_date=None)

    assert store_transactions(session, USER, master_key, CURRENT_ACCOUNT, [undated]) == (0, 0, 1)


def test_missing_booking_date_is_indexed_on_the_fallback_date(
    session: Session, master_key: str, linked_accounts
):
    store_transactions(
        session,
        USER,
        master_key,
        CURRENT_ACCOUNT,
        [_raw(booking_date=None, value_date=None, transaction_date="2026-07-15")],
    )

    (row,) = _rows(session, master_key, CURRENT_ACCOUNT)
    assert row.booking_date_enc is None
    assert row.period_bidx == hash_index("2026-07", master_key)
    assert decrypt_data(row.transaction_date_enc, master_key) == "2026-07-15"


def test_foreign_currency_keeps_its_own_currency_on_the_stored_row(
    session: Session, master_key: str, linked_accounts
):
    store_transactions(
        session,
        USER,
        master_key,
        CURRENT_ACCOUNT,
        [_raw(transaction_amount={"currency": "CHF", "amount": "12.63"})],
    )

    (row,) = _rows(session, master_key, CURRENT_ACCOUNT)
    assert decrypt_data(row.currency_enc, master_key) == "CHF"
    assert decrypt_data(row.amount_enc, master_key) == "12.63"


# ---------------------------------------------------------------------------
# store_transactions — level 3: cross-account, scoped to the user
# ---------------------------------------------------------------------------


def test_same_operation_on_card_and_current_account_is_deduplicated(
    session: Session, master_key: str, linked_accounts
):
    # The dominant case: 93 % of card operations also exist on the current
    # account, and none of them share a reference.
    store_transactions(session, USER, master_key, CURRENT_ACCOUNT, [_raw(entry_reference="current-ref")])

    result = store_transactions(session, USER, master_key, CARD_ACCOUNT, [_raw(entry_reference="card-ref")])

    assert result == (0, 0, 1)
    assert _rows(session, master_key, CARD_ACCOUNT) == []


def test_two_currencies_on_the_same_day_are_not_confused(session: Session, master_key: str, linked_accounts):
    # Ruling R11: the fingerprint carries the currency. The captured data holds
    # an unconverted CHF 12.63 debit; a EUR 12.63 debit on the same day and
    # direction is a different operation and must survive level 3.
    store_transactions(
        session,
        USER,
        master_key,
        CURRENT_ACCOUNT,
        [_raw(entry_reference="chf-1", transaction_amount={"currency": "CHF", "amount": "12.63"})],
    )

    result = store_transactions(
        session,
        USER,
        master_key,
        CARD_ACCOUNT,
        [_raw(entry_reference="eur-1", transaction_amount={"currency": "EUR", "amount": "12.63"})],
    )

    assert result == (1, 0, 0)
    (row,) = _rows(session, master_key, CARD_ACCOUNT)
    assert decrypt_data(row.currency_enc, master_key) == "EUR"


def test_cross_account_dedup_does_not_reach_another_user(session: Session, master_key: str, linked_accounts):
    other_account = "bank-account-other-user"
    other_session = BankSession(
        user_uuid_bidx=hash_index("user-2", master_key),
        session_id_enc=encrypt_data("eb-session-2", master_key),
        status="AUTHORIZED",
        consent_valid_until=datetime(2026, 12, 1, tzinfo=timezone.utc),
        authorized_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    session.add(other_session)
    session.commit()
    session.add(
        BankAccountLink(
            user_uuid_bidx=hash_index("user-2", master_key),
            bank_account_uuid_bidx=hash_index(other_account, master_key),
            session_uuid=other_session.uuid,
            identification_hash_bidx=hash_index("ident-other", master_key),
            account_uid_enc=encrypt_data("uid-other", master_key),
            anchor_date=date(2026, 8, 1),
            anchor_balance_enc=encrypt_data("1.00", master_key),
            last_synced_at=date(2026, 8, 1),
        )
    )
    session.commit()

    store_transactions(session, USER, master_key, CURRENT_ACCOUNT, [_raw(entry_reference="mine")])
    result = store_transactions(session, "user-2", master_key, other_account, [_raw(entry_reference="theirs")])

    assert result == (1, 0, 0)


def test_out_of_order_stream_produces_the_same_result(session: Session, master_key: str, linked_accounts):
    # "The order of transactions is not guaranteed" — nothing may depend on it.
    batch = [
        _raw(entry_reference="a", booking_date="2026-08-01", transaction_amount={"currency": "EUR", "amount": "1.00"}),
        _raw(entry_reference="b", booking_date="2026-06-20", transaction_amount={"currency": "EUR", "amount": "2.00"}),
        _raw(entry_reference="c", booking_date="2026-07-05", transaction_amount={"currency": "EUR", "amount": "3.00"}),
    ]

    assert store_transactions(session, USER, master_key, CURRENT_ACCOUNT, batch) == (3, 0, 0)
    shuffled = [batch[2], batch[0], batch[1]]
    assert store_transactions(session, USER, master_key, CURRENT_ACCOUNT, shuffled) == (0, 3, 0)

    periods = {row.period_bidx for row in _rows(session, master_key, CURRENT_ACCOUNT)}
    assert periods == {
        hash_index("2026-08", master_key),
        hash_index("2026-07", master_key),
        hash_index("2026-06", master_key),
    }


# ---------------------------------------------------------------------------
# Real captured payloads (skipped when vendor-docs is not checked out)
# ---------------------------------------------------------------------------


def test_real_current_account_payload_is_stored_in_full(session: Session, master_key: str, linked_accounts):
    raw_transactions = _load_spike("tx_courant.json")

    inserted, updated, skipped = store_transactions(
        session, USER, master_key, CURRENT_ACCOUNT, raw_transactions
    )

    assert (inserted, updated, skipped) == (len(raw_transactions), 0, 0)
    # Re-running the same feed must be a no-op in row count.
    assert store_transactions(session, USER, master_key, CURRENT_ACCOUNT, raw_transactions) == (
        0,
        len(raw_transactions),
        0,
    )


def test_real_card_payload_is_deduplicated_against_the_current_account(
    session: Session, master_key: str, linked_accounts
):
    current = _load_spike("tx_courant.json")
    card = _load_spike("tx_carte.json")
    store_transactions(session, USER, master_key, CURRENT_ACCOUNT, current)

    inserted, updated, skipped = store_transactions(session, USER, master_key, CARD_ACCOUNT, card)

    assert updated == 0
    assert inserted + skipped == len(card)
    # Measured on the captured payloads: 197 of the 202 card operations also
    # exist on the current account, none of them sharing a numeric reference.
    assert skipped == 197
    assert inserted == 5


def test_real_payload_pending_and_dateless_booking_are_handled(
    session: Session, master_key: str, linked_accounts
):
    raw_transactions = _load_spike("tx_courant.json")
    store_transactions(session, USER, master_key, CURRENT_ACCOUNT, raw_transactions)

    rows = _rows(session, master_key, CURRENT_ACCOUNT)
    statuses = [decrypt_data(row.status_enc, master_key) for row in rows]
    assert statuses.count("PDNG") == 3
    # The two operations with no booking_date still get a period, from their
    # transaction_date.
    without_booking = [row for row in rows if row.booking_date_enc is None]
    assert len(without_booking) == 2
    assert all(row.period_bidx for row in without_booking)
