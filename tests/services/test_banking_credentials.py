from sqlmodel import Session

from dtos.banking import BankConnectionUpdate
from models.banking import UserBankConnection
from services.banking.credentials import (
    delete_connection,
    get_connection,
    upsert_connection,
)
from services.encryption import hash_index


def test_upsert_connection_creates_with_application_id_and_key(session: Session, master_key: str):
    status = upsert_connection(
        session,
        "user_1",
        master_key,
        BankConnectionUpdate(application_id="app-123", private_key="-----BEGIN KEY-----secret"),
    )
    assert status.has_credentials is True
    assert status.application_id == "app-123"


def test_upsert_connection_stored_key_is_never_plaintext(session: Session, master_key: str):
    upsert_connection(
        session,
        "user_1",
        master_key,
        BankConnectionUpdate(application_id="app-123", private_key="super-secret-pem"),
    )
    row = get_connection(session, "user_1", master_key)
    assert row is not None
    assert row.private_key_enc != "super-secret-pem"
    assert row.application_id_enc != "app-123"
    assert "super-secret-pem" not in row.private_key_enc


def test_get_connection_reads_back_row(session: Session, master_key: str):
    upsert_connection(
        session,
        "user_1",
        master_key,
        BankConnectionUpdate(application_id="app-123", private_key="secret-key"),
    )
    row = get_connection(session, "user_1", master_key)
    assert row is not None
    assert row.user_uuid_bidx == hash_index("user_1", master_key)


def test_upsert_connection_field_absent_leaves_it_unchanged(session: Session, master_key: str):
    upsert_connection(
        session,
        "user_1",
        master_key,
        BankConnectionUpdate(application_id="app-123", private_key="secret-key"),
    )
    # Omitting private_key entirely must not touch it.
    status = upsert_connection(
        session,
        "user_1",
        master_key,
        BankConnectionUpdate(application_id="app-456"),
    )
    assert status.application_id == "app-456"
    assert status.has_credentials is True

    row = get_connection(session, "user_1", master_key)
    from services.encryption import decrypt_data
    assert decrypt_data(row.private_key_enc, master_key) == "secret-key"


def test_upsert_connection_empty_string_deletes_field(session: Session, master_key: str):
    upsert_connection(
        session,
        "user_1",
        master_key,
        BankConnectionUpdate(application_id="app-123", private_key="secret-key"),
    )
    status = upsert_connection(
        session,
        "user_1",
        master_key,
        BankConnectionUpdate(private_key=""),
    )
    assert status.has_credentials is False
    assert status.application_id == "app-123"

    row = get_connection(session, "user_1", master_key)
    assert row.private_key_enc is None


def test_delete_connection_removes_row_and_has_credentials_is_false(session: Session, master_key: str):
    upsert_connection(
        session,
        "user_1",
        master_key,
        BankConnectionUpdate(application_id="app-123", private_key="secret-key"),
    )
    delete_connection(session, "user_1", master_key)

    row = get_connection(session, "user_1", master_key)
    assert row is None


def test_get_connection_returns_none_when_never_configured(session: Session, master_key: str):
    assert get_connection(session, "brand_new_user", master_key) is None


def test_two_users_have_different_bidx_for_same_master_key(session: Session, master_key: str):
    upsert_connection(
        session,
        "user_a",
        master_key,
        BankConnectionUpdate(application_id="app-a", private_key="key-a"),
    )
    upsert_connection(
        session,
        "user_b",
        master_key,
        BankConnectionUpdate(application_id="app-b", private_key="key-b"),
    )
    row_a = get_connection(session, "user_a", master_key)
    row_b = get_connection(session, "user_b", master_key)
    assert row_a.user_uuid_bidx != row_b.user_uuid_bidx
