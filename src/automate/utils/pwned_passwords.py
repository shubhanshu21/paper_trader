"""
utils/pwned_passwords.py — Checks a candidate password against
HaveIBeenPwned's Pwned Passwords database via the k-anonymity range API
(https://haveibeenpwned.com/API/v3#PwnedPasswords). Only the first 5 hex
characters of the password's SHA-1 hash are ever sent over the network —
never the password itself or its full hash — so HIBP (or anyone
observing the request) can't recover the actual password from it.

Fails OPEN, never raises: a network failure or API outage must never
block registration — this is a defense-in-depth check layered on top of
the existing length/character rules (api/auth.py::validate_password_strength),
not a hard dependency for the app to function, same "never break the
flow" discipline this codebase already applies to notify.py/telegram_alert.py.
"""
import hashlib
import logging
from typing import Optional

import requests

log = logging.getLogger("api.auth")

_API_URL = "https://api.pwnedpasswords.com/range/{prefix}"
_TIMEOUT_SEC = 3


def check_password_pwned(password: str) -> Optional[int]:
    """
    Returns the number of times this password has appeared in known
    breaches, or None if it wasn't found OR the check couldn't be
    performed (network/API failure). Callers must treat None as "could
    not confirm either way" — never surface it to the user as "this
    password is safe."
    """
    sha1 = hashlib.sha1(password.encode("utf-8")).hexdigest().upper()
    prefix, suffix = sha1[:5], sha1[5:]

    try:
        resp = requests.get(_API_URL.format(prefix=prefix), timeout=_TIMEOUT_SEC)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("HaveIBeenPwned check failed (network/API) — skipping: %s", exc)
        return None

    for line in resp.text.splitlines():
        hash_suffix, _, count_str = line.partition(":")
        if hash_suffix == suffix:
            try:
                return int(count_str)
            except ValueError:
                return None
    return None
