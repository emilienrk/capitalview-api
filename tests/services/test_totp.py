import base64

from services.totp import (
    TOTP_DIGITS,
    build_otpauth_uri,
    generate_backup_codes,
    generate_recovery_key,
    generate_totp_secret,
    hash_backup_code,
    normalize_backup_code,
    normalize_recovery_key,
    totp_code,
    verify_totp,
)

# RFC 6238 Appendix B test vectors (SHA-1): ASCII secret "12345678901234567890"
_RFC_SECRET = base64.b32encode(b"12345678901234567890").decode("utf-8")
_RFC_VECTORS = [
    (59, "94287082"),
    (1111111109, "07081804"),
    (1111111111, "14050471"),
    (1234567890, "89005924"),
    (2000000000, "69279037"),
]


def test_rfc6238_vectors():
    # RFC vectors are 8 digits; compare the last TOTP_DIGITS digits
    for timestamp, expected in _RFC_VECTORS:
        assert totp_code(_RFC_SECRET, at=timestamp) == expected[-TOTP_DIGITS:]


def test_generate_totp_secret_format():
    secret = generate_totp_secret()
    padding = "=" * (-len(secret) % 8)
    assert len(base64.b32decode(secret + padding)) == 20
    assert secret != generate_totp_secret()


def test_verify_totp_current_and_window():
    secret = generate_totp_secret()
    now = 1_700_000_000
    code = totp_code(secret, at=now)
    # Exact step
    assert verify_totp(secret, code, at=now) == now // 30
    # Previous/next step within window ±1
    assert verify_totp(secret, code, at=now + 30) == now // 30
    assert verify_totp(secret, code, at=now - 30) == now // 30
    # Outside the window
    assert verify_totp(secret, code, at=now + 90) is None


def test_verify_totp_rejects_bad_input():
    secret = generate_totp_secret()
    assert verify_totp(secret, "12345", at=1_700_000_000) is None
    assert verify_totp(secret, "abcdef", at=1_700_000_000) is None
    assert verify_totp(secret, "", at=1_700_000_000) is None


def test_verify_totp_strips_spaces():
    secret = generate_totp_secret()
    now = 1_700_000_000
    code = totp_code(secret, at=now)
    spaced = f"{code[:3]} {code[3:]}"
    assert verify_totp(secret, spaced, at=now) is not None


def test_build_otpauth_uri():
    uri = build_otpauth_uri("ABC234", "user@example.com")
    assert uri.startswith("otpauth://totp/CapitalView%3Auser%40example.com?")
    assert "secret=ABC234" in uri
    assert "issuer=CapitalView" in uri
    assert "digits=6" in uri
    assert "period=30" in uri


def test_recovery_key_format_and_normalization():
    key = generate_recovery_key()
    groups = key.split("-")
    assert len(groups) == 8
    assert all(len(g) == 4 for g in groups)
    # Normalization strips separators and lowercases input is accepted
    assert normalize_recovery_key(key.lower().replace("-", " ")) == key.replace("-", "")
    assert normalize_recovery_key(key) == key.replace("-", "")


def test_backup_codes():
    codes = generate_backup_codes()
    assert len(codes) == 10
    assert len(set(codes)) == 10
    assert all(len(c) == 10 for c in codes)
    # Hash is deterministic and normalization-insensitive
    code = codes[0]
    assert hash_backup_code(code) == hash_backup_code(code.lower())
    assert hash_backup_code(code) == hash_backup_code(f" {code[:5]}-{code[5:]} ")
    assert hash_backup_code(code) != hash_backup_code(codes[1])
    assert normalize_backup_code(code.lower()) == code
