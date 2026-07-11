import pytest
import base64
import os
from unittest.mock import patch

from services.encryption import (
    init_salt,
    get_masterkey,
    generate_random_master_key,
    wrap_master_key,
    unwrap_master_key,
    server_encrypt,
    server_decrypt,
    derive_subkey_bytes,
    hash_password,
    hash_index,
    encrypt_data,
    decrypt_data,
    DecryptionError,
    NONCE_SIZE
)

def test_init_salt():
    salt = init_salt()
    assert isinstance(salt, str)
    assert len(base64.b64decode(salt)) > 0

def test_get_masterkey():
    salt = init_salt()
    password = "secret_password"
    mk = get_masterkey(password, salt)
    assert isinstance(mk, str)
    decoded = base64.b64decode(mk)
    assert len(decoded) == 32

def test_hash_password():
    pwd = "password123"
    hashed = hash_password(pwd)
    assert isinstance(hashed, str)
    assert hashed.startswith("$argon2id")

def test_derive_subkey_bytes():
    mk_bytes = os.urandom(32)
    mk = base64.b64encode(mk_bytes).decode("utf-8")
    k1 = derive_subkey_bytes(mk, "data")
    assert len(k1) == 32
    k2 = derive_subkey_bytes(mk, "index")
    assert len(k2) == 32
    assert k1 != k2
    with pytest.raises(ValueError):
        derive_subkey_bytes(mk, "invalid_context")

def test_hash_index():
    mk_bytes = os.urandom(32)
    mk = base64.b64encode(mk_bytes).decode("utf-8")
    uuid_val = "user-uuid-1234"
    idx1 = hash_index(uuid_val, mk)
    idx2 = hash_index(uuid_val, mk)
    assert idx1 == idx2
    assert idx1 != uuid_val

def test_encrypt_decrypt_cycle():
    mk_bytes = os.urandom(32)
    mk = base64.b64encode(mk_bytes).decode("utf-8")
    plaintext = "Hello World! This is secret."
    encrypted = encrypt_data(plaintext, mk)
    assert encrypted != plaintext
    decrypted = decrypt_data(encrypted, mk)
    assert decrypted == plaintext

def test_decrypt_tampered_data():
    mk_bytes = os.urandom(32)
    mk = base64.b64encode(mk_bytes).decode("utf-8")
    plaintext = "Sensitive Data"
    encrypted = encrypt_data(plaintext, mk)
    raw_bytes = list(base64.b64decode(encrypted))
    raw_bytes[-1] ^= 0xFF
    tampered_bytes = bytes(raw_bytes)
    tampered_b64 = base64.b64encode(tampered_bytes).decode("utf-8")
    with pytest.raises(DecryptionError):
        decrypt_data(tampered_b64, mk)

def test_decrypt_wrong_key():
    mk1_bytes = os.urandom(32)
    mk1 = base64.b64encode(mk1_bytes).decode("utf-8")
    mk2_bytes = os.urandom(32)
    mk2 = base64.b64encode(mk2_bytes).decode("utf-8")
    plaintext = "Data"
    encrypted = encrypt_data(plaintext, mk1)
    with pytest.raises(DecryptionError):
        decrypt_data(encrypted, mk2)


def test_generate_random_master_key():
    mk = generate_random_master_key()
    assert len(base64.b64decode(mk)) == 32
    assert mk != generate_random_master_key()


def test_wrap_unwrap_master_key_roundtrip():
    mk = generate_random_master_key()
    salt = init_salt()
    wrapped = wrap_master_key(mk, "MyPassword123!", salt)
    assert wrapped != mk
    assert unwrap_master_key(wrapped, "MyPassword123!", salt) == mk


def test_unwrap_master_key_wrong_secret():
    mk = generate_random_master_key()
    salt = init_salt()
    wrapped = wrap_master_key(mk, "MyPassword123!", salt)
    with pytest.raises(DecryptionError):
        unwrap_master_key(wrapped, "WrongPassword!", salt)


def test_unwrap_master_key_wrong_salt():
    mk = generate_random_master_key()
    salt = init_salt()
    wrapped = wrap_master_key(mk, "MyPassword123!", salt)
    with pytest.raises(DecryptionError):
        unwrap_master_key(wrapped, "MyPassword123!", init_salt())


def test_wrap_same_mk_as_legacy_derivation():
    """A legacy-derived MK can be wrapped and unwrapped unchanged (lazy migration)."""
    salt = init_salt()
    legacy_mk = get_masterkey("OldPassword1!", salt)
    wrap_salt = init_salt()
    wrapped = wrap_master_key(legacy_mk, "OldPassword1!", wrap_salt)
    assert unwrap_master_key(wrapped, "OldPassword1!", wrap_salt) == legacy_mk


def test_server_encrypt_decrypt_roundtrip():
    encrypted = server_encrypt("totp-secret-value", "totp")
    assert encrypted != "totp-secret-value"
    assert server_decrypt(encrypted, "totp") == "totp-secret-value"


def test_server_decrypt_wrong_context():
    encrypted = server_encrypt("totp-secret-value", "totp")
    with pytest.raises(DecryptionError):
        server_decrypt(encrypted, "2fa-pending")
