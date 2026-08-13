"""API token service: minting, authentication, revocation and expiry."""

import uuid as uuid_lib
from datetime import datetime, timedelta, timezone

import pytest

from models.api_token import ApiToken
from models.user import User
from services.api_token import (
    TOKEN_PREFIX,
    authenticate_api_token,
    create_api_token,
    decrypt_token_name,
    hash_api_token,
    list_api_tokens,
    revoke_api_token,
    revoke_user_api_tokens,
)
from services.encryption import hash_password, init_salt


@pytest.fixture(name="user")
def user_fixture(session) -> User:
    user = User(
        uuid=str(uuid_lib.uuid4()),
        auth_salt=init_salt(),
        username=f"tokenuser-{uuid_lib.uuid4().hex[:8]}",
        email=f"{uuid_lib.uuid4().hex[:8]}@example.com",
        password_hash=hash_password("Strongpass1!"),
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def test_created_token_authenticates_and_returns_the_master_key(session, user, master_key):
    _, token = create_api_token(session, user, master_key, name="Claude Desktop")

    principal = authenticate_api_token(session, token)

    assert principal is not None
    assert principal.user_uuid == user.uuid
    # The whole point: the token alone reconstitutes the key that decrypts data.
    assert principal.master_key == master_key
    assert principal.has_scope("read")


def test_token_is_never_stored_in_plaintext(session, user, master_key):
    record, token = create_api_token(session, user, master_key, name="n8n")

    assert token.startswith(TOKEN_PREFIX)
    assert record.token_hash == hash_api_token(token)
    assert token not in record.token_hash
    assert token not in record.mk_wrapped


def test_wrong_token_does_not_authenticate(session, user, master_key):
    create_api_token(session, user, master_key, name="real")

    assert authenticate_api_token(session, f"{TOKEN_PREFIX}not-a-real-token") is None


def test_token_without_the_prefix_is_rejected(session, user, master_key):
    _, token = create_api_token(session, user, master_key, name="prefixed")

    assert authenticate_api_token(session, token.removeprefix(TOKEN_PREFIX)) is None
    assert authenticate_api_token(session, "") is None


def test_a_row_whose_wrap_does_not_open_yields_no_principal(session, user, master_key):
    """The wrap is the real gate, not the hash lookup.

    Matching ``token_hash`` only selects a row; releasing the Master Key still
    requires the token to derive the KEK that opens ``mk_wrapped``. A row whose
    wrap was written under some other secret must authenticate nobody.
    """
    record, token = create_api_token(session, user, master_key, name="victim")
    other_record, _ = create_api_token(session, user, master_key, name="elsewhere")

    record.mk_wrapped = other_record.mk_wrapped
    record.mk_salt = other_record.mk_salt
    session.add(record)
    session.commit()

    assert authenticate_api_token(session, token) is None


def test_revoked_token_stops_authenticating(session, user, master_key):
    record, token = create_api_token(session, user, master_key, name="revoke me")

    assert revoke_api_token(session, user.uuid, record.uuid) is True
    assert authenticate_api_token(session, token) is None
    assert list_api_tokens(session, user.uuid) == []


def test_revoking_someone_elses_token_does_nothing(session, user, master_key):
    record, token = create_api_token(session, user, master_key, name="mine")

    assert revoke_api_token(session, "some-other-user-uuid", record.uuid) is False
    assert authenticate_api_token(session, token) is not None


def test_expired_token_stops_authenticating(session, user, master_key):
    record, token = create_api_token(session, user, master_key, name="short lived")
    record.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    session.add(record)
    session.commit()

    assert authenticate_api_token(session, token) is None


def test_token_of_a_disabled_account_stops_authenticating(session, user, master_key):
    _, token = create_api_token(session, user, master_key, name="disabled soon")

    user.is_active = False
    session.add(user)
    session.commit()

    assert authenticate_api_token(session, token) is None


def test_authentication_records_the_last_use(session, user, master_key):
    record, token = create_api_token(session, user, master_key, name="tracked")
    assert record.last_used_at is None

    authenticate_api_token(session, token)

    assert session.get(ApiToken, record.uuid).last_used_at is not None


def test_last_use_is_not_rewritten_on_every_call(session, user, master_key):
    """Read-only tool calls must not turn into a write on every request."""
    record, token = create_api_token(session, user, master_key, name="chatty")

    authenticate_api_token(session, token)
    first = session.get(ApiToken, record.uuid).last_used_at

    authenticate_api_token(session, token)

    assert session.get(ApiToken, record.uuid).last_used_at == first


def test_token_names_are_stored_encrypted(session, user, master_key):
    record, _ = create_api_token(session, user, master_key, name="Claude Desktop")

    assert "Claude Desktop" not in record.name_enc
    assert decrypt_token_name(record, master_key) == "Claude Desktop"


def test_an_unreadable_name_still_lists_the_token(session, user, master_key):
    """A label that will not decrypt must not hide a token from revocation."""
    record, _ = create_api_token(session, user, master_key, name="Claude Desktop")
    record.name_enc = "not-valid-ciphertext"

    assert decrypt_token_name(record, master_key) == "(nom illisible)"


def test_revoke_all_clears_every_live_token(session, user, master_key):
    _, first = create_api_token(session, user, master_key, name="one")
    _, second = create_api_token(session, user, master_key, name="two")

    assert revoke_user_api_tokens(session, user.uuid) == 2
    assert authenticate_api_token(session, first) is None
    assert authenticate_api_token(session, second) is None


def test_tokens_are_listed_newest_first(session, user, master_key):
    older, _ = create_api_token(session, user, master_key, name="older")
    newer, _ = create_api_token(session, user, master_key, name="newer")

    older.created_at = datetime.now(timezone.utc) - timedelta(days=1)
    session.add(older)
    session.commit()

    assert [t.uuid for t in list_api_tokens(session, user.uuid)] == [newer.uuid, older.uuid]
