"""
Model-level tests for BankAuthorization, BankSession, BankAccountLink.

Focus: uniqueness constraints enforced at the DB level, and the clear/encrypted
split — the columns the design spec calls out as deliberately plaintext must
be readable as-is straight out of the database, while everything else must
not be.
"""
from datetime import date, datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, select

from models.banking import BankAccountLink, BankAuthorization, BankSession
from services.encryption import decrypt_data, encrypt_data, hash_index


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# BankAuthorization
# ---------------------------------------------------------------------------


def test_bank_authorization_state_bidx_is_unique(session: Session, master_key: str):
    user_bidx = hash_index("user-1", master_key)
    state_bidx = hash_index("some-random-state", master_key)

    session.add(
        BankAuthorization(
            user_uuid_bidx=user_bidx,
            state_bidx=state_bidx,
            aspsp_name_enc=encrypt_data("Boursorama", master_key),
            aspsp_country_enc=encrypt_data("FR", master_key),
            expires_at=_now() + timedelta(minutes=10),
        )
    )
    session.commit()

    session.add(
        BankAuthorization(
            user_uuid_bidx=user_bidx,
            state_bidx=state_bidx,
            aspsp_name_enc=encrypt_data("Other Bank", master_key),
            aspsp_country_enc=encrypt_data("DE", master_key),
            expires_at=_now() + timedelta(minutes=10),
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
    # A failed flush poisons the session's transaction; roll back before the
    # fixture's own teardown tries to (otherwise: SAWarning, transaction already
    # deassociated from connection).
    session.rollback()


def test_bank_authorization_sensitive_fields_are_encrypted(session: Session, master_key: str):
    row = BankAuthorization(
        user_uuid_bidx=hash_index("user-1", master_key),
        state_bidx=hash_index("state-abc", master_key),
        aspsp_name_enc=encrypt_data("Boursorama", master_key),
        aspsp_country_enc=encrypt_data("FR", master_key),
        authorization_id_enc=encrypt_data("auth-123", master_key),
        expires_at=_now() + timedelta(minutes=10),
    )
    session.add(row)
    session.commit()

    raw = session.execute(
        sa.text(
            "SELECT aspsp_name_enc, aspsp_country_enc, authorization_id_enc "
            "FROM bank_authorizations WHERE id = :id"
        ),
        {"id": row.id},
    ).one()

    assert "Boursorama" not in raw[0]
    assert "FR" not in raw[1]
    assert "auth-123" not in raw[2]
    assert decrypt_data(raw[0], master_key) == "Boursorama"
    assert decrypt_data(raw[1], master_key) == "FR"
    assert decrypt_data(raw[2], master_key) == "auth-123"


# ---------------------------------------------------------------------------
# BankSession
# ---------------------------------------------------------------------------


def _make_session_row(master_key: str, **overrides) -> BankSession:
    defaults = dict(
        user_uuid_bidx=hash_index("user-1", master_key),
        session_id_enc=encrypt_data("eb-session-id", master_key),
        aspsp_name_enc=encrypt_data("Boursorama", master_key),
        aspsp_country_enc=encrypt_data("FR", master_key),
        status="AUTHORIZED",
        consent_valid_until=_now() + timedelta(days=90),
        authorized_at=_now(),
    )
    defaults.update(overrides)
    return BankSession(**defaults)


def test_bank_session_status_and_consent_valid_until_are_clear_text(session: Session, master_key: str):
    valid_until = _now().replace(microsecond=0) + timedelta(days=90)
    row = _make_session_row(master_key, status="AUTHORIZED", consent_valid_until=valid_until)
    session.add(row)
    session.commit()

    raw = session.execute(
        sa.text("SELECT status, consent_valid_until FROM bank_sessions WHERE uuid = :u"),
        {"u": row.uuid},
    ).one()

    # Plain equality, straight from the DB: a Master-Key-less job can read these.
    assert raw[0] == "AUTHORIZED"
    stored_valid_until = raw[1] if isinstance(raw[1], datetime) else datetime.fromisoformat(raw[1])
    if stored_valid_until.tzinfo is None:
        stored_valid_until = stored_valid_until.replace(tzinfo=timezone.utc)
    assert stored_valid_until == valid_until


def test_bank_session_identifiers_are_encrypted(session: Session, master_key: str):
    row = _make_session_row(master_key)
    session.add(row)
    session.commit()

    raw = session.execute(
        sa.text("SELECT session_id_enc, aspsp_name_enc FROM bank_sessions WHERE uuid = :u"),
        {"u": row.uuid},
    ).one()

    assert "eb-session-id" not in raw[0]
    assert "Boursorama" not in raw[1]
    assert decrypt_data(raw[0], master_key) == "eb-session-id"
    assert decrypt_data(raw[1], master_key) == "Boursorama"


# ---------------------------------------------------------------------------
# BankAccountLink
# ---------------------------------------------------------------------------


def _make_link(master_key: str, session_uuid: str, **overrides) -> BankAccountLink:
    defaults = dict(
        user_uuid_bidx=hash_index("user-1", master_key),
        bank_account_uuid_bidx=hash_index("bank-account-1", master_key),
        session_uuid=session_uuid,
        identification_hash_bidx=hash_index("identification-hash-1", master_key),
        account_uid_enc=encrypt_data("eb-account-uid", master_key),
        anchor_date=date(2026, 8, 1),
        anchor_balance_enc=encrypt_data("1234.56", master_key),
        last_synced_at=date(2026, 8, 1),
    )
    defaults.update(overrides)
    return BankAccountLink(**defaults)


def test_bank_account_link_bank_account_uuid_bidx_is_unique(session: Session, master_key: str):
    bank_session = _make_session_row(master_key)
    session.add(bank_session)
    session.commit()

    session.add(_make_link(master_key, bank_session.uuid))
    session.commit()

    # Same CapitalView account, different EB session data — still a conflict:
    # a bank_account can be attached only once.
    session.add(
        _make_link(
            master_key,
            bank_session.uuid,
            identification_hash_bidx=hash_index("identification-hash-2", master_key),
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_bank_account_link_anchor_date_and_last_synced_at_are_clear_text(session: Session, master_key: str):
    bank_session = _make_session_row(master_key)
    session.add(bank_session)
    session.commit()

    row = _make_link(
        master_key,
        bank_session.uuid,
        anchor_date=date(2026, 7, 15),
        last_synced_at=date(2026, 8, 16),
    )
    session.add(row)
    session.commit()

    raw = session.execute(
        sa.text("SELECT anchor_date, last_synced_at FROM bank_account_links WHERE uuid = :u"),
        {"u": row.uuid},
    ).one()

    assert str(raw[0]) == "2026-07-15"
    assert str(raw[1]) == "2026-08-16"


def test_bank_account_link_sensitive_fields_are_encrypted(session: Session, master_key: str):
    bank_session = _make_session_row(master_key)
    session.add(bank_session)
    session.commit()

    row = _make_link(master_key, bank_session.uuid)
    session.add(row)
    session.commit()

    raw = session.execute(
        sa.text("SELECT account_uid_enc, anchor_balance_enc FROM bank_account_links WHERE uuid = :u"),
        {"u": row.uuid},
    ).one()

    assert "eb-account-uid" not in raw[0]
    assert "1234.56" not in raw[1]
    assert decrypt_data(raw[0], master_key) == "eb-account-uid"
    assert decrypt_data(raw[1], master_key) == "1234.56"


def test_bank_account_link_reconciliation_gap_defaults_to_null(session: Session, master_key: str):
    bank_session = _make_session_row(master_key)
    session.add(bank_session)
    session.commit()

    row = _make_link(master_key, bank_session.uuid)
    session.add(row)
    session.commit()
    session.refresh(row)

    assert row.last_reconciliation_gap_enc is None


def test_deleting_bank_session_with_referencing_link_is_restricted(master_key: str):
    """A session is disposable credential state, not the link's owner (§B5):
    the link must survive session loss, so deleting a still-referenced session
    must be blocked rather than take the link down with it (ON DELETE RESTRICT).

    Needs a dedicated engine, not the shared `session` fixture: SQLite only
    honors `PRAGMA foreign_keys` outside an active transaction, and the shared
    fixture's connection already has one open (that's how its rollback-based
    isolation works) by the time a test body runs — the pragma would silently
    no-op there. Enabling it at connect-time, before any transaction starts, is
    the documented way to get SQLite to enforce FKs at all (off by default,
    unlike PostgreSQL in production, which always enforces them).
    """
    engine = sa.create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @sa.event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    SQLModel.metadata.create_all(engine)
    try:
        with Session(engine) as isolated_session:
            bank_session = _make_session_row(master_key)
            isolated_session.add(bank_session)
            isolated_session.commit()

            link = _make_link(master_key, bank_session.uuid)
            isolated_session.add(link)
            isolated_session.commit()

            isolated_session.delete(bank_session)
            with pytest.raises(IntegrityError):
                isolated_session.flush()
            isolated_session.rollback()

            # The link is untouched: still there, still pointing at the same session.
            isolated_session.expire_all()
            survivor = isolated_session.exec(
                select(BankAccountLink).where(BankAccountLink.uuid == link.uuid)
            ).one()
            assert survivor.session_uuid == bank_session.uuid
    finally:
        SQLModel.metadata.drop_all(engine)
        engine.dispose()
