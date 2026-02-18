import pytest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
import jwt
import nacl.pwhash
from fastapi import HTTPException
from sqlmodel import Session

from services.auth import (
    verify_password,
    create_access_token,
    create_refresh_token,
    decode_access_token,
    authenticate_user,
    create_refresh_token_db,
    verify_refresh_token,
    revoke_user_refresh_tokens,
    revoke_refresh_token,
    get_current_user,
    get_current_active_user,
    get_master_key
)
from models.user import User, RefreshToken
from config import get_settings

def hash_password(password: str) -> str:
    return nacl.pwhash.str(password.encode('utf-8')).decode('ascii')

def test_verify_password():
    password = "secure_password"
    pwhash = hash_password(password)
    assert verify_password(password, pwhash) is True
    assert verify_password("wrong_password", pwhash) is False
    assert verify_password(password, "$argon2id$invalidhash") is False


def test_create_and_decode_access_token():
    data = {"sub": "user_123", "role": "admin"}
    token = create_access_token(data)
    decoded = decode_access_token(token)
    assert decoded["sub"] == "user_123"
    assert decoded["role"] == "admin"
    assert decoded["type"] == "access"
    delta = timedelta(minutes=10)
    token_exp = create_access_token(data, expires_delta=delta)
    decoded_exp = decode_access_token(token_exp)
    expected_exp = datetime.now(timezone.utc) + delta
    assert decoded_exp["exp"] - expected_exp.timestamp() < 10
    with pytest.raises(jwt.InvalidTokenError):
        decode_access_token("invalid.token.string")
    settings = get_settings()
    bad_payload = {"sub": "user", "type": "refresh"}
    bad_token = jwt.encode(bad_payload, settings.secret_key, algorithm=settings.algorithm)
    with pytest.raises(jwt.InvalidTokenError, match="Invalid token type"):
        decode_access_token(bad_token)


def test_authenticate_user(session: Session):
    password = "mypassword"
    pwhash = hash_password(password)
    user = User(uuid=str(uuid.uuid4()), auth_salt="salt", username="testuser", email="test@example.com", password_hash=pwhash, is_active=True)
    session.add(user)
    session.commit()
    session.refresh(user)
    authenticated = authenticate_user(session, "test@example.com", password)
    assert authenticated is not None
    assert authenticated.uuid == user.uuid
    assert authenticate_user(session, "test@example.com", "wrong") is None
    assert authenticate_user(session, "wrong@example.com", password) is None
    user.is_active = False
    session.add(user)
    session.commit()
    assert authenticate_user(session, "test@example.com", password) is None


def test_refresh_token_lifecycle(session: Session):
    user_uuid = str(uuid.uuid4())
    user = User(uuid=user_uuid, auth_salt="s", username="u", email="e@e.com", password_hash="h", is_active=True)
    session.add(user)
    session.commit()
    token_str = create_refresh_token()
    rt = create_refresh_token_db(session, user_uuid, token_str)
    assert rt.token == token_str
    assert rt.user_uuid == user_uuid
    assert rt.revoked is False
    expires_at = rt.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    assert expires_at > datetime.now(timezone.utc)
    verified = verify_refresh_token(session, token_str)
    assert verified is not None
    assert verified.id == rt.id
    assert revoke_refresh_token(session, token_str) is True
    assert verify_refresh_token(session, token_str) is None
    assert revoke_refresh_token(session, "non_existent") is False
    t1 = create_refresh_token_db(session, user_uuid, "t1")
    t2 = create_refresh_token_db(session, user_uuid, "t2")
    other_uuid = str(uuid.uuid4())
    other_user = User(uuid=other_uuid, auth_salt="s", username="o", email="o@e.com", password_hash="h")
    session.add(other_user)
    session.commit()
    t3 = create_refresh_token_db(session, other_uuid, "t3")
    count = revoke_user_refresh_tokens(session, user_uuid)
    assert count == 2
    session.refresh(t1)
    session.refresh(t2)
    session.refresh(t3)
    assert t1.revoked is True
    assert t2.revoked is True
    assert t3.revoked is False


def test_verify_refresh_token_expired(session: Session):
    user_uuid = str(uuid.uuid4())
    user = User(uuid=user_uuid, auth_salt="s", username="u", email="e@e.com", password_hash="h")
    session.add(user)
    session.commit()
    token_str = "expired_token"
    rt = RefreshToken(user_uuid=user_uuid, token=token_str, expires_at=datetime.now(timezone.utc) - timedelta(days=1))
    session.add(rt)
    session.commit()
    assert verify_refresh_token(session, token_str) is None


def test_get_current_user(session: Session):
    user_uuid = str(uuid.uuid4())
    user = User(uuid=user_uuid, auth_salt="s", username="depuser", email="dep@test.com", password_hash="hash", is_active=True)
    session.add(user)
    session.commit()
    session.refresh(user)
    token = create_access_token({"sub": str(user.uuid)})
    creds = MagicMock()
    creds.credentials = token
    fetched_user = get_current_user(creds, session)
    assert fetched_user.uuid == user.uuid
    creds.credentials = "invalid"
    with pytest.raises(HTTPException) as exc:
        get_current_user(creds, session)
    assert exc.value.status_code == 401
    token_no_user = create_access_token({"sub": str(uuid.uuid4())})
    creds.credentials = token_no_user
    with pytest.raises(HTTPException) as exc:
        get_current_user(creds, session)
    assert exc.value.status_code == 401


def test_get_current_active_user():
    active_user = User(is_active=True)
    assert get_current_active_user(active_user) == active_user
    inactive_user = User(is_active=False)
    with pytest.raises(HTTPException) as exc:
        get_current_active_user(inactive_user)
    assert exc.value.status_code == 403


def test_get_master_key():
    import base64
    valid_key = base64.b64encode(b"0" * 32).decode("utf-8")

    # Cookie takes priority over header
    assert get_master_key(master_key_cookie=valid_key) == valid_key
    assert get_master_key(x_master_key=valid_key) == valid_key
    assert get_master_key(master_key_cookie=valid_key, x_master_key="other") == valid_key
    assert get_master_key(master_key_cookie=None, x_master_key=valid_key) == valid_key

    # Missing key
    with pytest.raises(HTTPException) as exc:
        get_master_key(None, None)
    assert exc.value.status_code == 400
    assert "Master Key missing" in exc.value.detail

    # Invalid base64
    with pytest.raises(HTTPException) as exc:
        get_master_key(master_key_cookie="not-valid-base64!!!")
    assert exc.value.status_code == 400
    assert "Invalid Master Key format" in exc.value.detail

    # Valid base64 but wrong length (16 bytes instead of 32)
    short_key = base64.b64encode(b"0" * 16).decode("utf-8")
    with pytest.raises(HTTPException) as exc:
        get_master_key(master_key_cookie=short_key)
    assert exc.value.status_code == 400
    assert "Invalid Master Key format" in exc.value.detail


def test_create_refresh_token_db_custom_expiry(session: Session):
    user_uuid = str(uuid.uuid4())
    user = User(uuid=user_uuid, auth_salt="s", username="u", email="ex@test.com", password_hash="h")
    session.add(user)
    session.commit()
    token = "token_custom"
    delta = timedelta(hours=1)
    rt = create_refresh_token_db(session, user_uuid, token, expires_delta=delta)
    expires_at = rt.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    assert expires_at > datetime.now(timezone.utc)
    diff = expires_at - datetime.now(timezone.utc)
    assert 3500 < diff.total_seconds() < 3700
