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
