import pytest
import base64
import os
from unittest.mock import patch

from services.encryption import (
    init_salt,
    get_masterkey,
    derive_subkey_bytes,
    hash_password,
    hash_index,
    encrypt_data,
    decrypt_data,
    NONCE_SIZE
)

def test_init_salt():
    salt = init_salt()
    assert isinstance(salt, str)
    # Check it's valid base64
    assert len(base64.b64decode(salt)) > 0

def test_get_masterkey():
    salt = init_salt()
    password = "secret_password"
    mk = get_masterkey(password, salt)
    assert isinstance(mk, str)
    # Should be 32 bytes encoded
    decoded = base64.b64decode(mk)
    assert len(decoded) == 32

def test_hash_password():
    pwd = "password123"
    hashed = hash_password(pwd)
    assert isinstance(hashed, str)
    assert hashed.startswith("$argon2id")

def test_derive_subkey_bytes():
    # Setup dummy master key
    mk_bytes = os.urandom(32)
    mk = base64.b64encode(mk_bytes).decode("utf-8")
    
    # 1. Data Context
    k1 = derive_subkey_bytes(mk, "data")
    assert len(k1) == 32
    
    # 2. Index Context
    k2 = derive_subkey_bytes(mk, "index")
    assert len(k2) == 32
    assert k1 != k2
    
    # 3. Invalid Context
    with pytest.raises(ValueError):
        derive_subkey_bytes(mk, "invalid_context")

def test_hash_index():
    mk_bytes = os.urandom(32)
    mk = base64.b64encode(mk_bytes).decode("utf-8")
    
    uuid_val = "user-uuid-1234"
    
    idx1 = hash_index(uuid_val, mk)
    idx2 = hash_index(uuid_val, mk)
    
    assert idx1 == idx2 # Deterministic
    assert idx1 != uuid_val

def test_encrypt_decrypt_cycle():
    mk_bytes = os.urandom(32)
    mk = base64.b64encode(mk_bytes).decode("utf-8")
    
    plaintext = "Hello World! This is secret."
    
    # Encrypt
    encrypted = encrypt_data(plaintext, mk)
    assert encrypted != plaintext
    
    # Decrypt
    decrypted = decrypt_data(encrypted, mk)
    assert decrypted == plaintext

def test_decrypt_tampered_data():
    mk_bytes = os.urandom(32)
    mk = base64.b64encode(mk_bytes).decode("utf-8")
    
    plaintext = "Sensitive Data"
    encrypted = encrypt_data(plaintext, mk)
    
    # Tamper with the ciphertext (base64 level)
    raw_bytes = list(base64.b64decode(encrypted))
    raw_bytes[-1] ^= 0xFF # Flip last bit
    tampered_bytes = bytes(raw_bytes)
    tampered_b64 = base64.b64encode(tampered_bytes).decode("utf-8")
    
    # Expect error message
    result = decrypt_data(tampered_b64, mk)
    assert result == "Error: Incorrect key or corrupted data."

def test_decrypt_wrong_key():
    mk1_bytes = os.urandom(32)
    mk1 = base64.b64encode(mk1_bytes).decode("utf-8")
    
    mk2_bytes = os.urandom(32)
    mk2 = base64.b64encode(mk2_bytes).decode("utf-8")
    
    plaintext = "Data"
    encrypted = encrypt_data(plaintext, mk1)
    
    result = decrypt_data(encrypted, mk2)
    assert result == "Error: Incorrect key or corrupted data."
