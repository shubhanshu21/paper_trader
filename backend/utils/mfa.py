"""
utils/mfa.py — TOTP (RFC 6238) two-factor auth helpers for panel login,
plus one-time backup/recovery codes for when the authenticator device is
unavailable. Wired into api/routes_auth.py's /mfa/* endpoints and the
login flow (User.mfa_enabled / mfa_secret / mfa_backup_codes_json).

Backup codes are stored as bcrypt hashes (reuses api/auth.py's password
hashing context) — never plaintext — same as the password itself; each
one is single-use, removed from the stored list the moment it's
consumed.
"""
import base64
import io
import json
import secrets

import pyotp
import qrcode

from api.auth import get_password_hash, verify_password

_ISSUER = "Automate Trading Panel"
_NUM_BACKUP_CODES = 8
_BACKUP_CODE_LENGTH = 10  # hex chars, e.g. "a3f9c1d0e2"


def generate_totp_secret() -> str:
    """A fresh base32 TOTP secret — not persisted until the enrolling user proves they've scanned it (see routes_auth.py::mfa_confirm)."""
    return pyotp.random_base32()


def provisioning_uri(secret: str, username: str) -> str:
    """otpauth:// URI an authenticator app (Google/Microsoft Authenticator, Authy, ...) can scan or import directly."""
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=_ISSUER)


def qr_code_data_uri(otpauth_uri: str) -> str:
    """Renders the otpauth:// URI as a scannable QR code, returned as a data: URI the frontend can drop straight into an <img src>."""
    img = qrcode.make(otpauth_uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def verify_totp_code(secret: str, code: str) -> bool:
    """valid_window=1 tolerates ~30s of clock drift between server and the user's device — standard TOTP practice."""
    code = (code or "").strip()
    if not code:
        return False
    return pyotp.TOTP(secret).verify(code, valid_window=1)


def generate_backup_codes() -> list[str]:
    """Plaintext codes shown to the user ONCE at enrollment time — caller hashes them before storing (see hash_backup_codes)."""
    return [secrets.token_hex(_BACKUP_CODE_LENGTH // 2) for _ in range(_NUM_BACKUP_CODES)]


def hash_backup_codes(codes: list[str]) -> str:
    """JSON list of bcrypt hashes, ready for User.mfa_backup_codes_json."""
    return json.dumps([get_password_hash(c) for c in codes])


def consume_backup_code(backup_codes_json: str | None, code: str) -> tuple[bool, str | None]:
    """
    Checks `code` against the stored hashed backup codes. On a match,
    returns (True, updated_json_with_that_code_removed) — the caller
    must persist the updated JSON so the code can't be reused. On no
    match (or no codes left), returns (False, None) — caller leaves the
    stored codes untouched.
    """
    if not backup_codes_json or not code:
        return False, None
    try:
        hashes: list[str] = json.loads(backup_codes_json)
    except json.JSONDecodeError:
        return False, None

    code = code.strip()
    for h in hashes:
        if verify_password(code, h):
            remaining = [x for x in hashes if x != h]
            return True, json.dumps(remaining)
    return False, None
