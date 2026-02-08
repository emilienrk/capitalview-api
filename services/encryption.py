"""
Encryption and hashing service for CapitalView.

Uses:
- Argon2id for password hashing
- HKDF (SHA256) for subkey derivation
- AES-256-GCM for symmetric encryption of sensitive data
- HMAC-SHA256 for blind indexing
"""

import base64
import os

import nacl.pwhash
import nacl.utils
from cryptography.hazmat.primitives import hashes, hmac
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

NONCE_SIZE = 12


def init_salt() -> str:
    """Generates a random salt (Base64) for key derivation."""
    salt = nacl.utils.random(nacl.pwhash.argon2id.SALTBYTES)
    return base64.b64encode(salt).decode("utf-8")


def get_masterkey(password: str, salt: str) -> str:
    """
    Derives a Master Key (32 bytes) from a user password.
    
    Args:
        password: User password in plaintext
        salt: Unique salt (Base64)
    
    Returns:
        Master Key encoded in Base64
    """
    password_bytes = password.encode("utf-8")
    salt_bytes = base64.b64decode(salt)

    masterkey_bytes = nacl.pwhash.argon2id.kdf(
        32,
        password_bytes,
        salt_bytes,
        opslimit=nacl.pwhash.argon2id.OPSLIMIT_INTERACTIVE,
        memlimit=nacl.pwhash.argon2id.MEMLIMIT_INTERACTIVE,
    )

    return base64.b64encode(masterkey_bytes).decode("utf-8")


def derive_subkey_bytes(masterkey: str, context: str) -> bytes:
    """
    Derives a specific subkey from the Master Key via HKDF.
    
    Args:
        masterkey: Master Key (Base64)
        context: "data" for encryption | "index" for blind indexing
    
    Returns:
        Subkey of 32 bytes
    """
    if context == "data":
        info_bytes = b"data-encryption-key"
    elif context == "index":
        info_bytes = b"blind-indexing-key"
    else:
        raise ValueError("Invalid context: 'data' or 'index' only.")

    master_key_bytes = base64.b64decode(masterkey)

    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=info_bytes,
    )
    return hkdf.derive(master_key_bytes)


def hash_password(password: str) -> str:
    """
    Hashes a password for authentication (Argon2id).
    
    Args:
        password: Password in plaintext
    
    Returns:
        Argon2id Hash (PHC format)
    """
    password_bytes = password.encode("utf-8")
    hashed = nacl.pwhash.str(
        password_bytes,
        opslimit=nacl.pwhash.argon2id.OPSLIMIT_INTERACTIVE,
        memlimit=nacl.pwhash.argon2id.MEMLIMIT_INTERACTIVE,
    )

    return hashed.decode("utf-8")


def hash_index(uuid: str, masterkey: str) -> str:
    """
    Generates a blind index via HMAC-SHA256.
    Allows search on encrypted data without revealing the value.
    
    Args:
        uuid: Unique identifier to index
        masterkey: Master Key (Base64)
    
    Returns:
        HMAC Hash (Base64)
    """
    subkey_bytes = derive_subkey_bytes(masterkey=masterkey, context="index")

    h = hmac.HMAC(subkey_bytes, hashes.SHA256())
    h.update(uuid.encode("utf-8"))
    signature = h.finalize()

    return base64.b64encode(signature).decode("utf-8")


def encrypt_data(data_string: str, masterkey: str) -> str:
    """
    Encrypts a string with AES-256-GCM.
    
    Args:
        data_string: Plaintext data
        masterkey: Master Key (Base64)
    
    Returns:
        Nonce (12 bytes) + Ciphertext (Base64)
    """
    privatekey_bytes = derive_subkey_bytes(masterkey=masterkey, context="data")
    aesgcm = AESGCM(privatekey_bytes)

    nonce = os.urandom(NONCE_SIZE)
    data_bytes = data_string.encode("utf-8")

    ciphertext = aesgcm.encrypt(nonce, data_bytes, None)
    packed_data = nonce + ciphertext

    return base64.b64encode(packed_data).decode("utf-8")

def decrypt_data(encrypted_data: str, masterkey: str) -> str:
    """
    Decrypts AES-256-GCM data.
    
    Args:
        encrypted_data: Nonce + Ciphertext (Base64)
        masterkey: Master Key (Base64)
    
    Returns:
        Plaintext data or error message
    """
    privatekey_bytes = derive_subkey_bytes(masterkey=masterkey, context="data")
    aesgcm = AESGCM(privatekey_bytes)

    packed_bytes = base64.b64decode(encrypted_data)

    nonce = packed_bytes[:NONCE_SIZE]
    ciphertext = packed_bytes[NONCE_SIZE:]

    try:
        return aesgcm.decrypt(nonce, ciphertext, None).decode("utf-8")
    except Exception:
        return "Error: Incorrect key or corrupted data."
