"""
Tests for Enable Banking JSON export import / history catch-up (Task 11).
"""

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlmodel import Session, select

from models.bank import BankAccount
from models.banking import BankAccountLink, BankSession, BankTransaction
from models.account_history import AccountHistory
from services.banking.export_import import import_enablebanking_export
from services.encryption import decrypt_data, encrypt_data, hash_index


USER_UUID = "test-export-user"
MASTER_KEY = "master-key-32-chars-long-test!!"
SPIKE_DIR = Path(__file__).resolve().parents[3] / "vendor-docs" / "spike"


@pytest.fixture
def sqlite_pg_insert(monkeypatch):
    """Replace pg_insert (PostgreSQL-specific) with a plain SA insert for SQLite
    tests — same workaround as tests/services/test_bank.py:30."""
    import sqlalchemy as sa

    def _fake(table):
        class _Stmt:
            def values(self, rows):
                self._rows = rows
                return self

            def on_conflict_do_nothing(self, **kwargs):
                return sa.insert(table).values(self._rows)

        return _Stmt()

    monkeypatch.setattr("services.bank.pg_insert", _fake)


def _setup_account_and_link(
    session: Session,
    master_key: str,
    ident_hash: str,
    account_name: str = "Compte Boursorama",
    cash_account_type: str = "CACC",
    user_uuid: str = USER_UUID,
) -> tuple[BankAccount, BankAccountLink]:
    user_bidx = hash_index(user_uuid, master_key)
    account = BankAccount(
        user_uuid_bidx=user_bidx,
        name_enc=encrypt_data(account_name, master_key),
        balance_enc=encrypt_data("500.00", master_key),
        account_type_enc=encrypt_data("CHECKING", master_key),
    )
    session.add(account)
    session.commit()
    session.refresh(account)

    bank_session = BankSession(
        user_uuid_bidx=user_bidx,
        session_id_enc=encrypt_data("sess-1", master_key),
        status="AUTHORIZED",
        consent_valid_until=datetime.now(timezone.utc) + timedelta(days=90),
        authorized_at=datetime.now(timezone.utc),
        accounts_enc=encrypt_data(
            json.dumps([{
                "identification_hash": ident_hash,
                "cash_account_type": cash_account_type,
            }]),
            master_key,
        ),
    )
    session.add(bank_session)
    session.commit()
    session.refresh(bank_session)

    link = BankAccountLink(
        user_uuid_bidx=user_bidx,
        bank_account_uuid_bidx=hash_index(account.uuid, master_key),
        session_uuid=bank_session.uuid,
        identification_hash_bidx=hash_index(ident_hash, master_key),
        account_uid_enc=encrypt_data("uid-1", master_key),
        anchor_date=date.today(),
        anchor_balance_enc=encrypt_data("500.00", master_key),
        last_synced_at=date.today() - timedelta(days=1),
    )
    session.add(link)
    session.commit()
    session.refresh(link)
    return account, link


class TestExportImport:
    def test_import_matches_by_identification_hash(
        self, session: Session, master_key: str, sqlite_pg_insert
    ):
        ident = "ih-bourso-123456"
        account, link = _setup_account_and_link(session, master_key, ident)

        export_data = {
            "accounts": [
                {
                    "info": {
                        "identification_hash": ident,
                        "cash_account_type": "CACC",
                        "account_id": {"iban": "FR76123456789"},
                    },
                    "transactions": [
                        {
                            "entry_reference": "ref-1",
                            "transaction_amount": {"currency": "EUR", "amount": "45.50"},
                            "credit_debit_indicator": "DBIT",
                            "status": "BOOK",
                            "booking_date": "2026-08-01",
                            "remittance_information": ["Courses supermarché"],
                        },
                        {
                            "entry_reference": "ref-2",
                            "transaction_amount": {"currency": "EUR", "amount": "1200.00"},
                            "credit_debit_indicator": "CRDT",
                            "status": "BOOK",
                            "booking_date": "2026-08-05",
                            "remittance_information": ["Virement reçu"],
                        },
                    ],
                    "balances": [
                        {
                            "balance_amount": {"currency": "EUR", "amount": "1654.50"},
                            "balance_type": "CLBD",
                            "reference_date": "2026-08-10",
                        }
                    ],
                }
            ]
        }

        resp = import_enablebanking_export(session, USER_UUID, master_key, export_data)

        assert resp.imported_accounts == 1
        assert resp.results[0].inserted == 2
        assert resp.results[0].bank_account_uuid == account.uuid
        assert resp.results[0].snapshots_written > 0

        # Verify transactions stored
        txs = session.exec(
            select(BankTransaction).where(
                BankTransaction.account_id_bidx == hash_index(account.uuid, master_key)
            )
        ).all()
        assert len(txs) == 2

    def test_unlinked_account_in_export_is_reported_cleanly(
        self, session: Session, master_key: str
    ):
        export_data = {
            "accounts": [
                {
                    "info": {
                        "identification_hash": "ih-unknown-account",
                        "account_id": {"iban": "FR76999999999"},
                    },
                    "transactions": [],
                }
            ]
        }

        resp = import_enablebanking_export(session, USER_UUID, master_key, export_data)
        assert resp.imported_accounts == 0
        assert len(resp.results) == 1
        assert resp.results[0].status == "unlinked"

    def test_deduplication_with_existing_api_transactions(
        self, session: Session, master_key: str, sqlite_pg_insert
    ):
        ident = "ih-bourso-dedup"
        account, link = _setup_account_and_link(session, master_key, ident)

        # Pre-seed one transaction already synced via API
        session.add(
            BankTransaction(
                account_id_bidx=hash_index(account.uuid, master_key),
                period_bidx=hash_index("2026-08", master_key),
                entry_ref_bidx=hash_index("ref-already-here", master_key),
                dedup_bidx=hash_index("2026-08-01|25.00|EUR|DBIT", master_key),
                amount_enc=encrypt_data("25.00", master_key),
                currency_enc=encrypt_data("EUR", master_key),
                credit_debit_enc=encrypt_data("DBIT", master_key),
                status_enc=encrypt_data("BOOK", master_key),
                booking_date_enc=encrypt_data("2026-08-01", master_key),
            )
        )
        session.commit()

        export_data = {
            "accounts": [
                {
                    "info": {"identification_hash": ident},
                    "transactions": [
                        {
                            "entry_reference": "ref-already-here",
                            "transaction_amount": {"currency": "EUR", "amount": "25.00"},
                            "credit_debit_indicator": "DBIT",
                            "status": "BOOK",
                            "booking_date": "2026-08-01",
                        },
                        {
                            "entry_reference": "ref-new-one",
                            "transaction_amount": {"currency": "EUR", "amount": "15.00"},
                            "credit_debit_indicator": "DBIT",
                            "status": "BOOK",
                            "booking_date": "2026-08-02",
                        },
                    ],
                }
            ]
        }

        resp = import_enablebanking_export(session, USER_UUID, master_key, export_data)
        assert resp.results[0].inserted == 1
        assert resp.results[0].updated == 1

        txs = session.exec(
            select(BankTransaction).where(
                BankTransaction.account_id_bidx == hash_index(account.uuid, master_key)
            )
        ).all()
        assert len(txs) == 2

    def test_card_account_does_not_write_retrospective_curve(
        self, session: Session, master_key: str, sqlite_pg_insert
    ):
        ident = "ih-bourso-card"
        account, link = _setup_account_and_link(
            session, master_key, ident, cash_account_type="CARD"
        )

        export_data = {
            "accounts": [
                {
                    "info": {"identification_hash": ident, "cash_account_type": "CARD"},
                    "transactions": [
                        {
                            "entry_reference": "card-tx-1",
                            "transaction_amount": {"currency": "EUR", "amount": "10.00"},
                            "credit_debit_indicator": "DBIT",
                            "status": "BOOK",
                            "booking_date": "2026-08-01",
                        }
                    ],
                    "balances": [
                        {
                            "balance_amount": {"currency": "EUR", "amount": "0.00"},
                            "balance_type": "OTHR",
                            "reference_date": "2026-08-10",
                        }
                    ],
                }
            ]
        }

        resp = import_enablebanking_export(session, USER_UUID, master_key, export_data)
        assert resp.results[0].snapshots_written == 0
        assert "Courbe rétrospective non écrite" in resp.results[0].detail

    def test_invalid_payload_raises_value_error(self, session: Session, master_key: str):
        with pytest.raises(ValueError, match="accounts"):
            import_enablebanking_export(session, USER_UUID, master_key, {"invalid": "payload"})


@pytest.mark.skipif(
    not (SPIKE_DIR / "export-boursorama-2022-2026.json").exists(),
    reason="Real export sample not available",
)
class TestRealExportReplay:
    def test_replay_full_bourso_export(
        self, session: Session, master_key: str, sqlite_pg_insert
    ):
        with open(SPIKE_DIR / "export-boursorama-2022-2026.json") as f:
            raw_export = json.load(f)

        cacc_ident = raw_export["accounts"][0]["info"]["identification_hash"]
        card_ident = raw_export["accounts"][1]["info"]["identification_hash"]

        acc_cacc, link_cacc = _setup_account_and_link(
            session, master_key, cacc_ident, "Boursorama Courant", cash_account_type="CACC"
        )
        acc_card, link_card = _setup_account_and_link(
            session, master_key, card_ident, "Boursorama Carte", cash_account_type="CARD"
        )

        resp = import_enablebanking_export(session, USER_UUID, master_key, raw_export)
        assert resp.imported_accounts == 2

        # 2776 transactions on current account
        assert resp.results[0].inserted == 2776
        # 1464 total transactions on card account, 1436 cross-deduplicated onto current account
        # => 28 inserted, 1436 skipped (28 + 1436 = 1464)
        assert resp.results[1].inserted == 28
        assert resp.results[1].skipped == 1436
        assert resp.results[1].inserted + resp.results[1].skipped == 1464

    def test_the_card_account_is_stored_last_whatever_the_file_order(
        self, session: Session, master_key: str, sqlite_pg_insert
    ):
        """Ruling R12, applied to an export instead of a live sync.

        Cross-account deduplication is asymmetric: whichever account is stored
        second loses the movements the first already claimed. The sync imposes
        current-before-card; an export file lists its accounts in whatever order
        the portal wrote them. Measured on this capture, storing the card first
        ends with 2 798 rows instead of 2 804 — six real operations gone and
        209,70 € of difference, silently.
        """
        with open(SPIKE_DIR / "export-boursorama-2022-2026.json") as f:
            raw_export = json.load(f)

        cacc_ident = raw_export["accounts"][0]["info"]["identification_hash"]
        card_ident = raw_export["accounts"][1]["info"]["identification_hash"]
        _setup_account_and_link(
            session, master_key, cacc_ident, "Boursorama Courant", cash_account_type="CACC"
        )
        _setup_account_and_link(
            session, master_key, card_ident, "Boursorama Carte", cash_account_type="CARD"
        )

        # The card listed first — the order the portal is free to produce.
        reversed_export = {"accounts": list(reversed(raw_export["accounts"]))}
        resp = import_enablebanking_export(session, USER_UUID, master_key, reversed_export)

        by_uuid = {r.bank_account_uuid: r for r in resp.results}
        current = next(r for r in resp.results if r.inserted == 2776)
        card = next(r for r in resp.results if r is not current)
        assert current.inserted == 2776
        assert card.inserted == 28
        assert card.skipped == 1436
        assert len(by_uuid) == 2


# ---------------------------------------------------------------------------
# A catch-up export is history, not "where we are now" (§D5, R7)
# ---------------------------------------------------------------------------


def _export(ident: str, balances: list[dict], tx_days_ago: int = 40) -> dict:
    day = (date.today() - timedelta(days=tx_days_ago)).isoformat()
    return {
        "accounts": [
            {
                "info": {"identification_hash": ident, "cash_account_type": "CACC"},
                "transactions": [
                    {
                        "entry_reference": "old-ref-1",
                        "transaction_amount": {"currency": "EUR", "amount": "20.00"},
                        "credit_debit_indicator": "DBIT",
                        "status": "BOOK",
                        "booking_date": day,
                    }
                ],
                "balances": balances,
            }
        ]
    }


def _clbd(amount: str, days_ago: int) -> dict:
    return {
        "balance_amount": {"currency": "EUR", "amount": amount},
        "balance_type": "CLBD",
        "reference_date": (date.today() - timedelta(days=days_ago)).isoformat(),
    }


class TestAnchorIsNeverWalkedBackwards:
    def test_an_export_older_than_the_anchor_writes_its_curve_but_keeps_the_anchor(
        self, session: Session, master_key: str, sqlite_pg_insert
    ):
        """The catch-up case the fixtures never covered: a link already synced.

        Regressing `anchor_date` re-labels reconciled days as estimated (R7
        derives "estimated" from it), and regressing `balance_updated_at`
        restores a stale balance as the account's current one — the precise
        pattern §D5 warns about.
        """
        ident = "ih-anchor-guard"
        account, link = _setup_account_and_link(session, master_key, ident)
        today = date.today()
        link.anchor_date = today
        link.last_synced_at = today
        session.add(link)
        account.balance_enc = encrypt_data("999.00", master_key)
        account.balance_updated_at = today
        session.add(account)
        session.commit()

        resp = import_enablebanking_export(
            session, USER_UUID, master_key, _export(ident, [_clbd("100.00", days_ago=30)])
        )

        session.refresh(link)
        session.refresh(account)
        assert link.anchor_date == today
        assert link.last_synced_at == today
        assert account.balance_updated_at == today
        assert decrypt_data(account.balance_enc, master_key) == "999.00"
        # The history it *is* authoritative over is still written.
        assert resp.results[0].snapshots_written > 0
        assert "conservés" in resp.results[0].detail

    def test_an_export_at_or_after_the_anchor_takes_the_anchor_over(
        self, session: Session, master_key: str, sqlite_pg_insert
    ):
        ident = "ih-anchor-advance"
        account, link = _setup_account_and_link(session, master_key, ident)
        link.anchor_date = date.today() - timedelta(days=60)
        link.last_synced_at = date.today() - timedelta(days=61)
        session.add(link)
        session.commit()

        ref_date = date.today() - timedelta(days=5)
        import_enablebanking_export(
            session, USER_UUID, master_key, _export(ident, [_clbd("100.00", days_ago=5)])
        )

        session.refresh(link)
        session.refresh(account)
        assert link.anchor_date == ref_date
        assert account.balance_updated_at == ref_date
        assert Decimal(decrypt_data(account.balance_enc, master_key)) == Decimal("100.00")

        # An import is not a sync. Moving `last_synced_at` up to meet the anchor
        # would clear the seeding marker and cost the account its deep history.
        assert link.last_synced_at == date.today() - timedelta(days=61)
        assert link.last_synced_at < link.anchor_date

    def test_an_export_dated_today_does_not_cancel_the_seeding_pass(
        self, session: Session, master_key: str, sqlite_pg_insert
    ):
        """The ordinary flow: connect the bank, then import the export just
        downloaded. Its reference date is today, which equals the bootstrap
        anchor — so the guard above does not catch it. If the import also moved
        `last_synced_at`, the two dates would meet, `seeding` would turn false,
        and the first sync would ask for `default` from the anchor instead of
        `longest` from 2000-01-01. Everything the export did not contain would be
        lost without a word, and the marker never comes back."""
        ident = "ih-seeding-today"
        account, link = _setup_account_and_link(session, master_key, ident)
        assert link.last_synced_at < link.anchor_date  # seeding, before anything

        import_enablebanking_export(
            session, USER_UUID, master_key, _export(ident, [_clbd("100.00", days_ago=0)])
        )

        session.refresh(link)
        assert link.last_synced_at < link.anchor_date


class TestWhichBalanceTheImportReads:
    def test_the_real_time_balance_is_never_taken_for_the_accounting_one(
        self, session: Session, master_key: str, sqlite_pg_insert
    ):
        """Constraint 9 / §F: XPCD is published alongside CLBD and comes first
        as often as not. `raw_balances[0]` is wrong one time in two."""
        ident = "ih-balance-order"
        account, link = _setup_account_and_link(session, master_key, ident)
        balances = [
            {
                "balance_amount": {"currency": "EUR", "amount": "244.07"},
                "balance_type": "XPCD",
                "reference_date": (date.today() - timedelta(days=5)).isoformat(),
            },
            _clbd("406.70", days_ago=5),
        ]

        import_enablebanking_export(session, USER_UUID, master_key, _export(ident, balances))

        session.refresh(account)
        assert Decimal(decrypt_data(account.balance_enc, master_key)) == Decimal("406.70")

    def test_an_export_with_only_a_real_time_balance_is_not_a_clean_import(
        self, session: Session, master_key: str, sqlite_pg_insert
    ):
        """No silent substitution: reporting `imported` with `snapshots_written: 0`
        is indistinguishable from a card account behaving correctly."""
        ident = "ih-balance-missing"
        account, link = _setup_account_and_link(session, master_key, ident)
        balances = [
            {
                "balance_amount": {"currency": "EUR", "amount": "244.07"},
                "balance_type": "XPCD",
                "reference_date": date.today().isoformat(),
            }
        ]

        resp = import_enablebanking_export(session, USER_UUID, master_key, _export(ident, balances))

        assert resp.results[0].status == "balance_unavailable"
        assert resp.results[0].snapshots_written == 0
        # The operations themselves were still ingested.
        assert resp.results[0].inserted == 1

    def test_a_foreign_currency_closing_balance_is_not_read_as_euros(
        self, session: Session, master_key: str, sqlite_pg_insert
    ):
        ident = "ih-balance-currency"
        account, link = _setup_account_and_link(session, master_key, ident)
        balances = [
            {
                "balance_amount": {"currency": "CHF", "amount": "406.70"},
                "balance_type": "CLBD",
                "reference_date": date.today().isoformat(),
            }
        ]

        resp = import_enablebanking_export(session, USER_UUID, master_key, _export(ident, balances))

        assert resp.results[0].status == "balance_unavailable"


def test_a_curve_that_fails_to_build_is_never_reported_as_imported(
    session: Session, master_key: str, sqlite_pg_insert, monkeypatch
):
    """A swallowed exception used to leave `imported` / `snapshots_written: 0`,
    with nothing in the logs and nothing in the response to tell them apart."""
    ident = "ih-curve-error"
    account, link = _setup_account_and_link(session, master_key, ident)

    def _boom(*args, **kwargs):
        raise RuntimeError("history window write failed")

    monkeypatch.setattr("services.banking.export_import.replace_history_window", _boom)

    resp = import_enablebanking_export(
        session, USER_UUID, master_key, _export(ident, [_clbd("100.00", days_ago=5)])
    )

    assert resp.results[0].status == "curve_error"
    assert resp.results[0].inserted == 1
    assert resp.results[0].snapshots_written == 0
