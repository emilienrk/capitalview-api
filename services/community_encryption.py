"""
Community encryption helpers.

Uses a server-side AES-256-GCM key (COMMUNITY_ENCRYPTION_KEY) so the
backend can decrypt shared positions when another user views a profile.

The key format is a 32-byte value encoded as Base64.
"""

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from config import get_settings

NONCE_SIZE = 12


def _get_community_key_bytes() -> bytes:
    """Return the raw 32-byte community key, or raise if not configured."""
    raw = get_settings().community_encryption_key
    if not raw:
        raise RuntimeError(
            "COMMUNITY_ENCRYPTION_KEY is not set. "
            "Generate one with: python -c \"import os,base64; print(base64.b64encode(os.urandom(32)).decode())\""
        )
    key_bytes = base64.b64decode(raw)
    if len(key_bytes) != 32:
        raise RuntimeError("COMMUNITY_ENCRYPTION_KEY must be exactly 32 bytes (Base64-encoded).")
    return key_bytes


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
