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

TODO(security): Add TOTP/MFA for admin accounts.
TODO(security): Add OAuth provider (e.g. Google) as alternative login.
TODO(security): Implement session revocation list for immediate invalidation.
TODO(security): Integrate HaveIBeenPwned API for leaked password detection.
"""
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Cookie, Depends, HTTPException, Request, status
from jose import JWTError, jwt
from passlib.context import CryptContext

from automate.config import PanelAuthConfig

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

def create_access_token(user_id: int, username: str, role: str) -> str:
    """
    Create a signed JWT access token.

    Claims:
    - sub: str(user_id)
    - username: the user's display name
    - role: 'admin' | 'viewer'
    - exp: UTC expiry (PanelAuthConfig.SESSION_HOURS from now)
    - iat: issued-at
    """
    secret = PanelAuthConfig.jwt_secret()
    now = datetime.now(timezone.utc)
    expire = now + timedelta(hours=PanelAuthConfig.SESSION_HOURS)
    payload = {
        "sub": str(user_id),
        "username": username,
        "role": role,
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
# CSRF protection — double-submit cookie pattern
# ---------------------------------------------------------------------------

def generate_csrf_token() -> str:
    """Generate a cryptographically random CSRF token."""
    return secrets.token_hex(32)


def validate_csrf(request: Request, csrf_cookie: Optional[str] = Cookie(None, alias="csrf_token")) -> None:
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
    session_cookie: Optional[str] = Cookie(None, alias="__Host-session"),
) -> Optional[str]:
    """Extract JWT from Authorization header or __Host-session HttpOnly cookie."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
        if token:
            return token
    return session_cookie


def get_current_user_optional(
    token: Optional[str] = Depends(_get_token_from_cookie),
) -> Optional[dict]:
    """
    Dependency: return the decoded JWT payload if a valid session cookie exists,
    or None if not authenticated. Suitable for endpoints that degrade gracefully.
    """
    if not token:
        return None
    try:
        return decode_access_token(token)
    except JWTError:
        return None


def get_current_user(
    payload: Optional[dict] = Depends(get_current_user_optional),
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


def get_current_user_ws(websocket) -> Optional[dict]:
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
        return decode_access_token(token)
    except JWTError:
        return None


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
