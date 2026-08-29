"""
Enable Banking connection credentials (BYO application_id + private key).

Each CapitalView user brings their own Enable Banking application: the free
tier only exposes accounts linked by the account holder themselves. One
connection row per user.
"""

from sqlmodel import Session, select

from dtos.banking import BankConnectionStatus, BankConnectionUpdate
from models.banking import UserBankConnection
from services.encryption import decrypt_data, encrypt_data, hash_index


def get_connection(session: Session, user_uuid: str, master_key: str) -> UserBankConnection | None:
    """Return the connection row for a user, or None if never configured."""
    user_bidx = hash_index(user_uuid, master_key)
    return session.exec(
        select(UserBankConnection).where(UserBankConnection.user_uuid_bidx == user_bidx)
    ).first()


def _map_to_status(row: UserBankConnection | None, master_key: str) -> BankConnectionStatus:
    if row is None:
        return BankConnectionStatus(has_credentials=False, application_id=None)
    application_id = (
        decrypt_data(row.application_id_enc, master_key) if row.application_id_enc else None
    )
    return BankConnectionStatus(
        has_credentials=bool(row.application_id_enc and row.private_key_enc),
        application_id=application_id,
    )


def get_status(session: Session, user_uuid: str, master_key: str) -> BankConnectionStatus:
    """Read-only status for GET /banking/status — same mapping upsert_connection returns."""
    return _map_to_status(get_connection(session, user_uuid, master_key), master_key)


def upsert_connection(
    session: Session,
    user_uuid: str,
    master_key: str,
    data: BankConnectionUpdate,
) -> BankConnectionStatus:
    """
    Upsert the application_id and/or private key.

    Follows update_ai_provider exactly: field absent from the payload = unchanged,
    empty string = deletion. The private key is never returned.
    """
    user_bidx = hash_index(user_uuid, master_key)
    row = session.exec(
        select(UserBankConnection).where(UserBankConnection.user_uuid_bidx == user_bidx)
    ).first()

    if "application_id" in data.model_fields_set:
        if data.application_id and data.application_id.strip():
            new_application_id_enc = encrypt_data(data.application_id.strip(), master_key)
        else:
            new_application_id_enc = None
    else:
        new_application_id_enc = row.application_id_enc if row else None

    if "private_key" in data.model_fields_set:
        if data.private_key and data.private_key.strip():
            new_private_key_enc = encrypt_data(data.private_key.strip(), master_key)
        else:
            new_private_key_enc = None
    else:
        new_private_key_enc = row.private_key_enc if row else None

    if row is None:
        row = UserBankConnection(
            user_uuid_bidx=user_bidx,
            application_id_enc=new_application_id_enc,
            private_key_enc=new_private_key_enc,
        )
    else:
        row.application_id_enc = new_application_id_enc
        row.private_key_enc = new_private_key_enc

    session.add(row)
    session.commit()
    session.refresh(row)

    return _map_to_status(row, master_key)


def delete_connection(session: Session, user_uuid: str, master_key: str) -> None:
    """Delete the user's Enable Banking connection row entirely, if any."""
    row = get_connection(session, user_uuid, master_key)
    if row is not None:
        session.delete(row)
        session.commit()


def _decrypt_row(row: UserBankConnection | None, master_key: str) -> tuple[str, str] | None:
    if row is None or not row.application_id_enc or not row.private_key_enc:
        return None
    return (
        decrypt_data(row.application_id_enc, master_key),
        decrypt_data(row.private_key_enc, master_key),
    )


def get_decrypted_credentials(
    session: Session, user_uuid: str, master_key: str
) -> tuple[str, str] | None:
    """Return (application_id, private_key) decrypted, or None if not fully configured.

    Extracted per Task 1's review ruling once more than one caller needed the
    same application_id_enc/private_key_enc decryption (linking.py's config
    check, ASPSP catalogue, authorization and rattachement flows).
    """
    return _decrypt_row(get_connection(session, user_uuid, master_key), master_key)


def get_decrypted_credentials_by_bidx(
    session: Session, user_uuid_bidx: str, master_key: str
) -> tuple[str, str] | None:
    """Same as get_decrypted_credentials, keyed by an already-computed blind index.

    The OAuth callback route authenticates via `state`, not a Bearer token, so
    it never recovers the caller's raw user_uuid — only the user_uuid_bidx
    stashed on the BankAuthorization row it consumes.
    """
    row = session.exec(
        select(UserBankConnection).where(UserBankConnection.user_uuid_bidx == user_uuid_bidx)
    ).first()
    return _decrypt_row(row, master_key)
