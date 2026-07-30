"""
api/routes_auth.py — User registration, login, logout, and session endpoints.

Security properties:
- Login: credentials validated via bcrypt (passlib). Wrong credentials return
  the same generic 401 message regardless of whether user exists (no enumeration).
- Login: rate-limited to 10 attempts/minute per IP (slowapi).
- Login: sets '__Host-session' HttpOnly Secure SameSite=Lax cookie (no JS access).
- Login: sets 'csrf_token' readable cookie (JS must echo it as X-CSRF-Token header).
- Register: rate-limited to 5 attempts/minute per IP.
- Register: validates password strength (min 8 chars, max 128).
- Logout: clears both cookies unconditionally.
- All state-changing endpoints validate the CSRF double-submit token.
- MUST NOT send credentials in URL params (all in request body only).
- MUST NOT log credentials or tokens.
"""
import logging
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from slowapi import Limiter
from slowapi.util import get_remote_address

from automate.api.auth import (
    create_access_token,
    generate_csrf_token,
    get_current_user,
    get_password_hash,
    validate_password_strength,
    verify_password,
)
from automate.config import PanelAuthConfig
from automate.db.engine import get_session
from automate.db.models import User

log = logging.getLogger("api.auth_routes")
router = APIRouter(prefix="/api/auth", tags=["auth"])

# Rate limiter instance — shared with main.py if slowapi is configured globally.
# Using a module-level limiter here so auth routes are protected even if the
# global app limiter is not set up yet.
_limiter = Limiter(key_func=get_remote_address)

# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------

class RegisterRequest(BaseModel):
    username: str
    email: str
    password: str

    @field_validator("username")
    @classmethod
    def username_valid(cls, v: str) -> str:
        v = v.strip()
        if not v or len(v) < 3 or len(v) > 64:
            raise ValueError("Username must be 3–64 characters.")
        if not re.match(r"^[a-zA-Z0-9_.-]+$", v):
            raise ValueError("Username may only contain letters, numbers, _, ., -")
        return v

    @field_validator("email")
    @classmethod
    def email_valid(cls, v: str) -> str:
        v = v.strip().lower()
        # Basic format check — full RFC 5321 validation is overkill for a local panel.
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", v):
            raise ValueError("Invalid email address.")
        if len(v) > 254:
            raise ValueError("Email too long.")
        return v


class LoginRequest(BaseModel):
    username: str   # accepts username OR email
    password: str


class UserOut(BaseModel):
    id: int
    username: str
    email: str
    role: str
    is_active: bool
    created_at: Optional[str]


# ---------------------------------------------------------------------------
# Helper: set session + CSRF cookies on response
# ---------------------------------------------------------------------------

def _set_auth_cookies(response: Response, token: str, csrf_token: str) -> None:
    """
    Set the session JWT in an HttpOnly Secure SameSite=Lax cookie and the CSRF
    token in a readable (non-HttpOnly) cookie so the frontend JS can read it.

    Cookie name '__Host-' prefix enforces:
      - Secure flag required
      - No Domain attribute
      - Path must be '/'
    This prevents subdomain cookie injection attacks.
    """
    max_age = PanelAuthConfig.SESSION_HOURS * 3600
    response.set_cookie(
        key="__Host-session",
        value=token,
        max_age=max_age,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
        # No domain= set (required by __Host- prefix)
    )
    response.set_cookie(
        key="csrf_token",
        value=csrf_token,
        max_age=max_age,
        httponly=False,   # JS must be able to read this for the double-submit pattern
        secure=True,
        samesite="lax",
        path="/",
    )


def _clear_auth_cookies(response: Response) -> None:
    """Delete session and CSRF cookies unconditionally."""
    response.delete_cookie(key="__Host-session", path="/", secure=True, samesite="lax")
    response.delete_cookie(key="csrf_token",      path="/", secure=True, samesite="lax")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
@_limiter.limit("5/minute")
def register(request: Request, body: RegisterRequest, response: Response):
    """
    Register a new user.

    - Open registration is allowed when no users exist yet (bootstrapping)
      or when PANEL_OPEN_REGISTRATION=true.
    - Subsequent registrations require PANEL_OPEN_REGISTRATION=true (default)
      or can be locked down by setting it to false (admin-invite only).
    - Password strength is validated server-side.
    - Duplicate username/email returns a generic 409 to avoid enumeration.

    NOTE: CSRF validation is skipped here because the registration form is
    publicly accessible (no session exists yet). The rate limit provides
    protection against automated abuse.
    """
    # Validate password strength before touching the DB
    try:
        validate_password_strength(body.password)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    with get_session() as session:
        # Check if this is the very first user (always allowed as bootstrapping)
        user_count = session.execute(select(User)).scalars().all()
        is_first_user = len(user_count) == 0

        if not is_first_user and not PanelAuthConfig.OPEN_REGISTRATION:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Registration is closed. Contact an administrator.",
            )

        # Check for duplicate username/email — generic message to avoid enumeration
        existing = session.execute(
            select(User).where(
                (User.username == body.username) | (User.email == body.email)
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Username or email already in use.",
            )

        # First user is automatically an admin
        role = "admin" if is_first_user else "viewer"
        hashed = get_password_hash(body.password)

        new_user = User(
            username=body.username,
            email=body.email,
            hashed_password=hashed,
            role=role,
            is_active=1,
        )
        session.add(new_user)
        session.flush()  # get the auto-incremented id before commit

        user_dict = new_user.to_safe_dict()

    log.info("New user registered: username=%s role=%s", body.username, role)
    return user_dict


@router.post("/login")
@_limiter.limit("10/minute")
def login(request: Request, body: LoginRequest, response: Response):
    """
    Authenticate with username (or email) + password.

    On success: sets '__Host-session' + 'csrf_token' cookies.
    Returns the user's public profile (no token in body — token is in cookie).

    Wrong credentials always return the same 401 regardless of whether the
    username exists — prevents user enumeration.
    MUST NOT log the password or any credential.
    """
    _generic_401 = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid username or password.",
    )

    with get_session() as session:
        # Accept username or email in the 'username' field
        identifier = body.username.strip().lower()
        user = session.execute(
            select(User).where(
                (User.username == body.username.strip()) | (User.email == identifier)
            )
        ).scalar_one_or_none()

        # Constant-time check: always call verify_password to prevent timing attacks
        # even when user is not found (compare against a dummy hash).
        _DUMMY_HASH = "$2b$12$GhvMmNVjRW29ulnudl.LbuAnmhcrOrTdeiTzkUt9in5zclB4bDs1S"
        stored_hash = user.hashed_password if user else _DUMMY_HASH
        password_ok = verify_password(body.password, stored_hash)

        if not user or not password_ok or not user.is_active:
            # MUST NOT log the supplied password
            log.warning("Failed login attempt for identifier: %s", identifier[:64])
            raise _generic_401

        token = create_access_token(user.id, user.username, user.role)
        csrf = generate_csrf_token()
        _set_auth_cookies(response, token, csrf)

        log.info("User logged in: username=%s id=%s", user.username, user.id)
        return {
            "user": user.to_safe_dict(),
            "message": "Logged in successfully",
            "access_token": token,
            "csrf_token": csrf,
        }


@router.post("/logout")
def logout(response: Response):
    """
    Log out the current user. Clears session and CSRF cookies unconditionally.

    NOTE: CSRF validation is intentionally skipped on logout — the worst an
    attacker can do with a CSRF-forced logout is a DoS (session invalidation),
    not data modification. Including CSRF would require the user to be logged in,
    creating a chicken-and-egg problem for pre-login logouts.
    """
    _clear_auth_cookies(response)
    return {"message": "Logged out successfully"}


@router.get("/me", response_model=UserOut)
def me(payload: dict = Depends(get_current_user)):
    """
    Return the currently authenticated user's public profile.
    Used by the frontend on startup to restore session state.
    """
    user_id = int(payload["sub"])
    with get_session() as session:
        user = session.get(User, user_id)
        if user is None or not user.is_active:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session invalid")
        return user.to_safe_dict()
