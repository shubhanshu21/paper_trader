"""
api/routes_oauth.py — "Sign in with Google" for the panel, an alternative
to username/password login. Entirely optional (see config.GoogleOAuthConfig)
— every endpoint here 503s with a clear message if not configured, and the
rest of the app (including plain username/password login) is completely
unaffected either way; nothing here is on the startup/import critical path.

Standard OAuth 2.0 Authorization Code flow:
  1. GET .../login — redirects the browser to Google's consent screen,
     with a signed 'state' param (see auth.py::create_oauth_state_token
     — CSRF protection: prevents an attacker from tricking a victim into
     completing an attacker-initiated OAuth flow and getting the
     victim's browser bound to the attacker's Google account).
  2. Google redirects back to .../callback with a 'code' (+ the same
     'state').
  3. We verify 'state', exchange 'code' for Google's tokens, fetch the
     user's verified email from Google's userinfo endpoint, and either
     log in the matching existing account or auto-provision a new one
     (role='viewer' unless it's the very first user, same bootstrapping
     rule as routes_auth.py::register — a random, never-usable-for-
     login password hash, since this account only ever authenticates
     via Google).
  4. Set the same session cookies login() does, redirect into the app.
"""
import logging
import re
import secrets
import urllib.parse
from typing import Optional

import requests
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from automate.api.auth import (
    create_access_token,
    create_oauth_state_token,
    generate_csrf_token,
    get_password_hash,
    verify_oauth_state_token,
)
from automate.api.routes_auth import _set_auth_cookies
from automate.config import GoogleOAuthConfig
from automate.db.engine import get_session
from automate.db.models import User

log = logging.getLogger("api.oauth")
router = APIRouter(prefix="/api/auth/oauth/google", tags=["auth"])

_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
_TIMEOUT_SEC = 10


def _not_configured() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail=(
            "Google sign-in isn't configured on this server. The operator needs to set "
            "GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET / GOOGLE_OAUTH_REDIRECT_URI "
            "(see config.py::GoogleOAuthConfig) using a real OAuth client registered in "
            "Google Cloud Console — this application cannot create that registration itself."
        ),
    )


def _unique_username_from_email(session: Session, email: str) -> str:
    """Derives a username satisfying routes_auth.py::RegisterRequest's rules from the Google account's email local-part, de-duplicated if already taken."""
    local_part = email.split("@", 1)[0]
    base = re.sub(r"[^a-zA-Z0-9_.-]", "", local_part) or "user"
    base = base[:60]

    candidate = base
    suffix = 0
    while session.execute(select(User).where(User.username == candidate)).scalar_one_or_none() is not None:
        suffix += 1
        candidate = f"{base}{suffix}"[:64]
    return candidate


@router.get("/status")
def oauth_status():
    """Lets the frontend hide the 'Sign in with Google' button entirely when unconfigured, rather than showing one that would 503 on click."""
    return {"configured": GoogleOAuthConfig.is_configured()}


@router.get("/login")
def oauth_login():
    if not GoogleOAuthConfig.is_configured():
        raise _not_configured()

    state = create_oauth_state_token()
    params = {
        "client_id": GoogleOAuthConfig.CLIENT_ID,
        "redirect_uri": GoogleOAuthConfig.REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    }
    return RedirectResponse(f"{_AUTH_URL}?{urllib.parse.urlencode(params)}")


@router.get("/callback")
def oauth_callback(code: Optional[str] = Query(None), state: Optional[str] = Query(None), error: Optional[str] = Query(None)):
    if not GoogleOAuthConfig.is_configured():
        raise _not_configured()

    if error:
        log.warning("Google OAuth callback returned an error: %s", error)
        return RedirectResponse("/login?oauth_error=" + urllib.parse.quote(error))

    if not code or not state or not verify_oauth_state_token(state):
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state — please try signing in again.")

    try:
        token_resp = requests.post(
            _TOKEN_URL,
            data={
                "code": code,
                "client_id": GoogleOAuthConfig.CLIENT_ID,
                "client_secret": GoogleOAuthConfig.CLIENT_SECRET,
                "redirect_uri": GoogleOAuthConfig.REDIRECT_URI,
                "grant_type": "authorization_code",
            },
            timeout=_TIMEOUT_SEC,
        )
        token_resp.raise_for_status()
        access_token = token_resp.json().get("access_token")
        if not access_token:
            raise requests.RequestException("Token response had no access_token")

        userinfo_resp = requests.get(
            _USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}, timeout=_TIMEOUT_SEC,
        )
        userinfo_resp.raise_for_status()
        userinfo = userinfo_resp.json()
    except requests.RequestException as exc:
        log.error("Google OAuth token/userinfo exchange failed: %s", exc)
        raise HTTPException(status_code=502, detail="Could not complete Google sign-in — please try again.")

    email = (userinfo.get("email") or "").strip().lower()
    if not email or not userinfo.get("email_verified", False):
        raise HTTPException(status_code=403, detail="Your Google account has no verified email address.")

    with get_session() as session:
        user = session.execute(select(User).where(User.email == email)).scalar_one_or_none()
        if user is None:
            is_first_user = len(session.execute(select(User)).scalars().all()) == 0
            user = User(
                username=_unique_username_from_email(session, email),
                email=email,
                # This account only ever authenticates via Google — the
                # hash is random and never told to anyone, so a password-
                # based login attempt can never succeed against it.
                hashed_password=get_password_hash(secrets.token_urlsafe(32)),
                role="admin" if is_first_user else "viewer",
                is_active=1,
            )
            session.add(user)
            session.flush()
            log.info("New user auto-provisioned via Google OAuth: username=%s email=%s", user.username, email)
        elif not user.is_active:
            raise HTTPException(status_code=403, detail="This account has been deactivated.")

        token = create_access_token(user.id, user.username, user.role, user.token_version)
        csrf = generate_csrf_token()

        redirect = RedirectResponse(url="/dashboard")
        _set_auth_cookies(redirect, token, csrf)
        log.info("User logged in via Google OAuth: username=%s id=%s", user.username, user.id)
        return redirect
