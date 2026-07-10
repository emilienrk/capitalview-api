"""
TOTP (RFC 6238), recovery key and backup code helpers.

Implemented with the standard library only (hmac/hashlib/struct/base64):
- TOTP: HMAC-SHA1, 30-second time step, 6 digits (compatible with
  Google Authenticator, Aegis, 2FAS, etc.)
- Recovery key: 8 groups of 4 Crockford-Base32 characters (160 bits)
- Backup codes: single-use codes stored as HMAC-SHA256(SECRET_KEY) hashes
"""

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

from config import get_settings

TOTP_STEP_SECONDS = 30
TOTP_DIGITS = 6

# Crockford Base32 alphabet (no I/L/O/U — unambiguous when read aloud)
_RECOVERY_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_RECOVERY_GROUPS = 8
_RECOVERY_GROUP_LEN = 4

_BACKUP_CODE_LEN = 10
_BACKUP_CODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"  # standard base32 chars


# ── TOTP (RFC 4226 / 6238) ───────────────────────────────────

def generate_totp_secret() -> str:
    """Generates a random TOTP secret (20 bytes, Base32 without padding)."""
    return base64.b32encode(secrets.token_bytes(20)).decode("utf-8").rstrip("=")


def _hotp(secret_b32: str, counter: int) -> str:
    """RFC 4226 HOTP value for a given counter."""
    # Re-pad the Base32 secret if padding was stripped
    padding = "=" * (-len(secret_b32) % 8)
    key = base64.b32decode(secret_b32.upper() + padding)

    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()

    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** TOTP_DIGITS)).zfill(TOTP_DIGITS)


def totp_code(secret_b32: str, at: int | None = None) -> str:
    """Returns the 6-digit TOTP code for the given Unix timestamp (default: now)."""
    timestamp = int(time.time()) if at is None else at
    return _hotp(secret_b32, timestamp // TOTP_STEP_SECONDS)


def verify_totp(secret_b32: str, code: str, window: int = 1, at: int | None = None) -> int | None:
    """
    Verifies a TOTP code within ±window time steps.

    Returns:
        The accepted time step (for replay protection) or None if invalid.
    """
    code = code.strip().replace(" ", "")
    if len(code) != TOTP_DIGITS or not code.isdigit():
        return None

    timestamp = int(time.time()) if at is None else at
    current_step = timestamp // TOTP_STEP_SECONDS

    for offset in range(-window, window + 1):
        step = current_step + offset
        if step < 0:
            continue
        if hmac.compare_digest(_hotp(secret_b32, step), code):
            return step
    return None


def build_otpauth_uri(secret_b32: str, email: str, issuer: str = "CapitalView") -> str:
    """Builds the otpauth:// provisioning URI (rendered as a QR code by the frontend)."""
    label = quote(f"{issuer}:{email}")
    return (
        f"otpauth://totp/{label}"
        f"?secret={secret_b32}&issuer={quote(issuer)}"
        f"&algorithm=SHA1&digits={TOTP_DIGITS}&period={TOTP_STEP_SECONDS}"
    )


# ── Recovery key ─────────────────────────────────────────────

def generate_recovery_key() -> str:
    """Generates a readable recovery key: 8 groups of 4 Crockford-Base32 chars (160 bits)."""
    groups = [
        "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(_RECOVERY_GROUP_LEN))
        for _ in range(_RECOVERY_GROUPS)
    ]
    return "-".join(groups)


def normalize_recovery_key(raw: str) -> str:
    """Normalizes user input: uppercase, strips separators and whitespace."""
    return "".join(c for c in raw.upper() if c in _RECOVERY_ALPHABET)


# ── Backup codes ─────────────────────────────────────────────

def generate_backup_codes(n: int = 10) -> list[str]:
    """Generates *n* single-use backup codes (10 Base32 chars each, ~50 bits)."""
    return [
        "".join(secrets.choice(_BACKUP_CODE_ALPHABET) for _ in range(_BACKUP_CODE_LEN))
        for _ in range(n)
    ]


def normalize_backup_code(raw: str) -> str:
    """Normalizes a backup code: uppercase, strips separators and whitespace."""
    return "".join(c for c in raw.upper() if c in _BACKUP_CODE_ALPHABET)


def hash_backup_code(code: str) -> str:
    """HMAC-SHA256 of a backup code with SECRET_KEY, for storage and O(1) lookup."""
    settings = get_settings()
    return hmac.new(
        settings.secret_key.encode("utf-8"),
        normalize_backup_code(code).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
