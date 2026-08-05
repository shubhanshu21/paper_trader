"""
api/auth.py — JWT + bcrypt helpers and FastAPI dependencies for panel auth.

Security notes:
- Passwords hashed with bcrypt (work factor 12) via passlib.
- JWT tokens use HS256, secret from PanelAuthConfig.jwt_secret() (multi-tiered
  resolution: env var → secret file → ephemeral random + warning).
- Tokens carried in '__Host-session' HttpOnly, Secure, SameSite=Lax cookies.
- CSRF protection via double-submit cookie: the client must echo the CSRF
  token from the 'csrf_token' cookie in the 'X-CSRF-Token' request header
  on every state-changing (non-GET) request.
- 'none' JWT algorithm explicitly rejected.
- 'exp' claim validated on every decode.
- Session revocation: every token embeds the issuing user's token_version
  ('tv' claim); get_current_user_optional() checks it against the live DB
  value on every request, so bump_token_version() invalidates every
  outstanding session for that user immediately, not just at natural
  'exp' — used by "log out everywhere" and account deactivation.
- Registration rejects passwords found in known public breaches (see
  utils/pwned_passwords.py, called from routes_auth.py::register) — on
  top of validate_password_strength()'s length/character rules below.
- TOTP two-factor auth is available per-account (see utils/mfa.py,
  routes_auth.py's /mfa/* endpoints) — opt-in, not mandatory.
- "Sign in with Google" is available if the operator configures a real
  Google OAuth client (see config.GoogleOAuthConfig, api/routes_oauth.py)
  — off by default, and the rest of the app is unaffected either way.
"""
import logging
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import Cookie, Depends, HTTPException, Request, status
from jose import JWTError, jwt
from passlib.context import CryptContext

from config import PanelAuthConfig

log = logging.getLogger("api.auth")

# ---------------------------------------------------------------------------
# Password hashing — bcrypt, work factor 12
# ---------------------------------------------------------------------------
_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=12)

ALGORITHM = "HS256"  # explicitly hardcoded — never derived from token header


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plaintext password against a bcrypt hash."""
    return _pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    """Hash a password with bcrypt (unique per-call salt baked in)."""
    return _pwd_context.hash(password)


# ---------------------------------------------------------------------------
# JWT token creation and verification
# ---------------------------------------------------------------------------

def create_access_token(user_id: int, username: str, role: str, token_version: int = 0) -> str:
    """
    Create a signed JWT access token.

    Claims:
    - sub: str(user_id)
    - username: the user's display name
    - role: 'admin' | 'viewer'
    - tv: the user's token_version at issue time — checked against the
      live DB value on every request (see get_current_user_optional);
      a mismatch means this token was revoked after being issued.
    - exp: UTC expiry (PanelAuthConfig.SESSION_HOURS from now)
    - iat: issued-at
    """
    secret = PanelAuthConfig.jwt_secret()
    now = datetime.now(UTC)
    expire = now + timedelta(hours=PanelAuthConfig.SESSION_HOURS)
    payload = {
        "sub": str(user_id),
        "username": username,
        "role": role,
        "tv": token_version,
        "iat": now,
        "exp": expire,
    }
    return jwt.encode(payload, secret, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict:
    """
    Decode and validate a JWT access token.

    Raises JWTError on any validation failure (expired, bad signature,
    wrong algorithm, missing claims).
    """
    secret = PanelAuthConfig.jwt_secret()
    # algorithms list is hardcoded — never trust token header for algorithm.
    payload = jwt.decode(token, secret, algorithms=[ALGORITHM])
    if payload.get("sub") is None:
        raise JWTError("Token missing 'sub' claim")
    return payload


# ---------------------------------------------------------------------------
# MFA-pending tokens — issued after a correct password but before the TOTP/
# backup code step, for accounts with mfa_enabled. Deliberately a SEPARATE
# token shape (short-lived, carries only 'purpose': 'mfa_pending', no role/
# tv claims) rather than a real access token with reduced permissions — so
# it can never accidentally be accepted by get_current_user_optional() as a
# real session, even if the two code paths drift in the future. See
# routes_auth.py's login()/mfa_verify_login().
# ---------------------------------------------------------------------------
_MFA_PENDING_MINUTES = 5


def create_mfa_pending_token(user_id: int) -> str:
    secret = PanelAuthConfig.jwt_secret()
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "purpose": "mfa_pending",
        "iat": now,
        "exp": now + timedelta(minutes=_MFA_PENDING_MINUTES),
    }
    return jwt.encode(payload, secret, algorithm=ALGORITHM)


def decode_mfa_pending_token(token: str) -> dict:
    """Raises JWTError if invalid, expired, or not actually an mfa_pending token."""
    secret = PanelAuthConfig.jwt_secret()
    payload = jwt.decode(token, secret, algorithms=[ALGORITHM])
    if payload.get("sub") is None or payload.get("purpose") != "mfa_pending":
        raise JWTError("Not a valid MFA-pending token")
    return payload


# ---------------------------------------------------------------------------
# OAuth state tokens — the 'state' param round-tripped through Google's
# consent screen (api/routes_oauth.py), verified on callback to prevent
# CSRF (an attacker tricking a victim into completing an attacker-
# initiated OAuth flow, binding the victim's session to the attacker's
# Google account). Stateless by design (a signed, short-lived JWT, not a
# server-side session store) — this app has no shared cache/Redis, and a
# single web process doesn't need one just for a 10-minute CSRF token.
# ---------------------------------------------------------------------------
_OAUTH_STATE_MINUTES = 10


def create_oauth_state_token() -> str:
    secret = PanelAuthConfig.jwt_secret()
    now = datetime.now(UTC)
    payload = {"purpose": "oauth_state", "iat": now, "exp": now + timedelta(minutes=_OAUTH_STATE_MINUTES)}
    return jwt.encode(payload, secret, algorithm=ALGORITHM)


def verify_oauth_state_token(token: str) -> bool:
    try:
        secret = PanelAuthConfig.jwt_secret()
        payload = jwt.decode(token, secret, algorithms=[ALGORITHM])
        return payload.get("purpose") == "oauth_state"
    except JWTError:
        return False


# ---------------------------------------------------------------------------
# CSRF protection — double-submit cookie pattern
# ---------------------------------------------------------------------------

def generate_csrf_token() -> str:
    """Generate a cryptographically random CSRF token."""
    return secrets.token_hex(32)


def validate_csrf(request: Request, csrf_cookie: str | None = Cookie(None, alias="csrf_token")) -> None:
    """
    Validate the CSRF double-submit cookie.

    The client must:
    1. Read the 'csrf_token' cookie (accessible via JS — not HttpOnly).
    2. Echo it in the 'X-CSRF-Token' request header on every state-changing request.

    We compare the cookie value to the header value. If they don't match (or
    either is absent), we reject the request with 403.
    """
    # Bypass CSRF validation if request is authenticated via Bearer token
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return

    header_token = request.headers.get("X-CSRF-Token", "")
    if not csrf_cookie or not header_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF token missing",
        )
    if not secrets.compare_digest(csrf_cookie, header_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF token mismatch",
        )


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------

def _get_token_from_cookie(
    request: Request,
    session_cookie: str | None = Cookie(None, alias="__Host-session"),
) -> str | None:
    """Extract JWT from Authorization header or __Host-session HttpOnly cookie."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
        if token:
            return token
    return session_cookie


def get_current_user_optional(
    token: str | None = Depends(_get_token_from_cookie),
) -> dict | None:
    """
    Dependency: return the decoded JWT payload if a valid, non-revoked
    session exists, or None if not authenticated/revoked. Suitable for
    endpoints that degrade gracefully.

    Checks the token's 'tv' claim against the live token_version column
    (see User model) — this is what makes bump_token_version() actually
    revoke a session immediately instead of only at 'exp'. A missing 'tv'
    claim (a token issued before this check existed) is treated as 0,
    matching the column's default, so already-issued tokens keep working
    until they naturally expire rather than being force-invalidated by
    this change.
    """
    if not token:
        return None
    try:
        payload = decode_access_token(token)
    except JWTError:
        return None

    if payload.get("purpose") == "mfa_pending":
        return None  # never accept an MFA-pending token as a real session

    if not _token_version_is_current(payload):
        return None
    return payload


def _token_version_is_current(payload: dict) -> bool:
    from db.engine import get_session
    from db.models import User

    try:
        user_id = int(payload["sub"])
    except (KeyError, TypeError, ValueError):
        return False
    token_version = payload.get("tv", 0)

    with get_session() as session:
        user = session.get(User, user_id)
        if user is None or not user.is_active:
            return False
        return user.token_version == token_version


def bump_token_version(session, user: "User") -> None:  # noqa: F821 — type-only forward ref, avoids a hard import cycle
    """
    Revoke every currently-outstanding session for `user` immediately —
    used by "log out everywhere" and account deactivation. `session` must
    be an open Session the caller commits (matches this module's other
    DB-touching helpers, which never own the transaction themselves).
    """
    user.token_version = (user.token_version or 0) + 1


def get_current_user(
    payload: dict | None = Depends(get_current_user_optional),
) -> dict:
    """
    Dependency: require a valid session. Raises 401 if not authenticated.
    Use this as the auth gate on all protected endpoints.
    """
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Cookie"},
        )
    return payload


def require_admin(
    payload: dict = Depends(get_current_user),
) -> dict:
    """
    Dependency: require admin role. Raises 403 if the user is not an admin.
    """
    if payload.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return payload


def get_current_user_ws(websocket) -> dict | None:
    """
    WebSocket equivalent of get_current_user_optional() — the global
    require_auth_middleware in main.py only wraps regular HTTP requests
    (Starlette's @app.middleware("http") never sees the websocket upgrade
    request), so every /ws/* endpoint has to check the session cookie
    itself if it wants to know (or require) who's connected. Returns None
    if there's no valid session; callers that need per-user data must
    close the socket themselves rather than serve unscoped data.
    """
    token = websocket.cookies.get("__Host-session")
    if not token:
        return None
    try:
        payload = decode_access_token(token)
    except JWTError:
        return None
    if not _token_version_is_current(payload):
        return None
    return payload


# ---------------------------------------------------------------------------
# Password strength validation
# ---------------------------------------------------------------------------
_MIN_PASSWORD_LENGTH = 8
_MAX_PASSWORD_LENGTH = 128


def validate_password_strength(password: str) -> None:
    """
    Enforce minimum password requirements.
    - At least 8 characters (12+ recommended).
    - No maximum length below 128 chars.
    - All characters allowed (including special chars).
    Raises ValueError with a descriptive message on failure.
    """
    if len(password) < _MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {_MIN_PASSWORD_LENGTH} characters.")
    if len(password) > _MAX_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at most {_MAX_PASSWORD_LENGTH} characters.")
