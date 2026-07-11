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

from config import get_settings

NONCE_SIZE = 12


class DecryptionError(Exception):
    """Raised when AES-GCM decryption fails (wrong key or corrupted/tampered data)."""


def _get_community_key_bytes() -> bytes:
    """Return the raw 32-byte community key, or raise if not configured.

    Accepts the key either as Base64-encoded (44 chars with padding) or as
    hexadecimal (64 lowercase hex chars = 32 bytes).
    """
    raw = get_settings().community_encryption_key
    if not raw:
        raise RuntimeError(
            "COMMUNITY_ENCRYPTION_KEY is not set. "
            "Generate one with: python -c \"import os,base64; print(base64.b64encode(os.urandom(32)).decode())\""
        )
    # Detect hex encoding: exactly 64 hexadecimal characters
    if len(raw) == 64 and all(c in "0123456789abcdefABCDEF" for c in raw):
        import binascii
        key_bytes = binascii.unhexlify(raw)
    else:
        key_bytes = base64.b64decode(raw)
    if len(key_bytes) != 32:
        raise RuntimeError("COMMUNITY_ENCRYPTION_KEY must be exactly 32 bytes (hex or Base64-encoded).")
    return key_bytes


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


def generate_random_master_key() -> str:
    """Generates a random Master Key (32 bytes, Base64), independent from the password."""
    return base64.b64encode(os.urandom(32)).decode("utf-8")


def derive_kek(secret: str, salt: str) -> bytes:
    """
    Derives a Key-Encryption-Key (32 bytes) from a secret (password or recovery key).

    Args:
        secret: Secret in plaintext (password or normalized recovery key)
        salt: Unique salt (Base64)

    Returns:
        Raw 32-byte KEK
    """
    secret_bytes = secret.encode("utf-8")
    salt_bytes = base64.b64decode(salt)

    return nacl.pwhash.argon2id.kdf(
        32,
        secret_bytes,
        salt_bytes,
        opslimit=nacl.pwhash.argon2id.OPSLIMIT_INTERACTIVE,
        memlimit=nacl.pwhash.argon2id.MEMLIMIT_INTERACTIVE,
    )


def wrap_master_key(masterkey: str, secret: str, salt: str) -> str:
    """
    Wraps (encrypts) the Master Key with a KEK derived from a secret.

    Args:
        masterkey: Master Key (Base64)
        secret: Secret used to derive the KEK (password or recovery key)
        salt: Salt for the KEK derivation (Base64)

    Returns:
        Base64(nonce ‖ AES-256-GCM ciphertext of the raw MK bytes)
    """
    kek = derive_kek(secret, salt)
    aesgcm = AESGCM(kek)
    nonce = os.urandom(NONCE_SIZE)
    ciphertext = aesgcm.encrypt(nonce, base64.b64decode(masterkey), None)
    return base64.b64encode(nonce + ciphertext).decode("utf-8")


def unwrap_master_key(wrapped: str, secret: str, salt: str) -> str:
    """
    Unwraps (decrypts) a wrapped Master Key.

    Raises:
        DecryptionError: if the secret is wrong or the data is corrupted.
    """
    kek = derive_kek(secret, salt)
    aesgcm = AESGCM(kek)
    packed = base64.b64decode(wrapped)
    nonce = packed[:NONCE_SIZE]
    ciphertext = packed[NONCE_SIZE:]
    try:
        mk_bytes = aesgcm.decrypt(nonce, ciphertext, None)
    except Exception as exc:
        raise DecryptionError("Incorrect secret or corrupted data.") from exc
    return base64.b64encode(mk_bytes).decode("utf-8")


def _derive_server_key(context: str) -> bytes:
    """Derives a server-side 32-byte key from SECRET_KEY via HKDF for the given context."""
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=f"server-{context}".encode("utf-8"),
    )
    return hkdf.derive(get_settings().secret_key.encode("utf-8"))


def server_encrypt(plaintext: str, context: str) -> str:
    """Encrypts *plaintext* with a server key derived from SECRET_KEY (AES-256-GCM).

    Used for secrets that must be readable by the server without the user's
    Master Key (e.g. TOTP secrets, which are verified before the MK is released).
    """
    aesgcm = AESGCM(_derive_server_key(context))
    nonce = os.urandom(NONCE_SIZE)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.b64encode(nonce + ciphertext).decode("utf-8")


def server_decrypt(encrypted: str, context: str) -> str:
    """Decrypts data produced by :func:`server_encrypt` with the same context.

    Raises:
        DecryptionError: if the context/key is wrong or the data is corrupted.
    """
    aesgcm = AESGCM(_derive_server_key(context))
    packed = base64.b64decode(encrypted)
    nonce = packed[:NONCE_SIZE]
    ciphertext = packed[NONCE_SIZE:]
    try:
        return aesgcm.decrypt(nonce, ciphertext, None).decode("utf-8")
    except Exception as exc:
        raise DecryptionError("Incorrect key or corrupted data.") from exc


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
        Plaintext data

    Raises:
        DecryptionError: if the key is incorrect or the data is corrupted/tampered.
    """
    privatekey_bytes = derive_subkey_bytes(masterkey=masterkey, context="data")
    aesgcm = AESGCM(privatekey_bytes)

    packed_bytes = base64.b64decode(encrypted_data)

    nonce = packed_bytes[:NONCE_SIZE]
    ciphertext = packed_bytes[NONCE_SIZE:]

    try:
        return aesgcm.decrypt(nonce, ciphertext, None).decode("utf-8")
    except Exception as exc:
        raise DecryptionError("Incorrect key or corrupted data.") from exc


def community_encrypt(plaintext: str) -> str:
    """Encrypt *plaintext* with the community key (AES-256-GCM).

    Returns Base64(nonce ‖ ciphertext).
    """
    key = _get_community_key_bytes()
    aesgcm = AESGCM(key)
    nonce = os.urandom(NONCE_SIZE)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.b64encode(nonce + ciphertext).decode("utf-8")


def community_decrypt(encrypted: str) -> str:
    """Decrypt Base64(nonce ‖ ciphertext) with the community key.

    Raises on invalid data / wrong key.
    """
    key = _get_community_key_bytes()
    aesgcm = AESGCM(key)
    packed = base64.b64decode(encrypted)
    nonce = packed[:NONCE_SIZE]
    ciphertext = packed[NONCE_SIZE:]
    return aesgcm.decrypt(nonce, ciphertext, None).decode("utf-8")
