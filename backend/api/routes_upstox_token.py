"""
api/routes_upstox_token.py — read-only status + manual "refresh now" for
the Upstox access token, surfaced as a small header badge in the frontend
(see TopNav.tsx's UpstoxTokenBadge). Deliberately separate from
api/token_refresh_scheduler.py's own automatic loop — this only ever
exposes it, never replaces it; the scheduler keeps running exactly as
before regardless of whether anyone ever looks at or clicks this badge.
"""
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError

from fastapi import APIRouter, Depends

from api.auth import get_current_user
from auth.upstox_auto_login import ensure_fresh_upstox_token, token_status
from utils.logger import get_logger

router = APIRouter(prefix="/api/upstox-token", tags=["upstox-token"])
log = get_logger(__name__)

# Same bounded-timeout contract as token_refresh_scheduler.py's own
# _refresh_bounded — a stuck Selenium session must never hang this HTTP
# request forever. Deliberately NOT importing that private helper (it's
# tied to the scheduler's own executor lifecycle); this is its own
# single-purpose executor for the manual "refresh now" click.
_REFRESH_TIMEOUT_SEC = 300


@router.get("/status")
def get_token_status(user: dict = Depends(get_current_user)):
    """
    {"configured": bool, "valid": bool, "expiry_epoch": float | None} —
    see upstox_auto_login.token_status(). `valid` is a real live check
    (one LTP call), not a locally-guessed one — this route is meant to be
    polled occasionally by a header badge, not hammered; the frontend
    should poll on a coarse interval (see TopNav.tsx).
    """
    return token_status()


@router.post("/refresh")
def refresh_token_now(user: dict = Depends(get_current_user)):
    """
    Manually trigger a token refresh — same ensure_fresh_upstox_token(force=True)
    the scheduler's own proactive-refresh path uses, run off the event
    loop so a slow/stuck Selenium session can't block this request past
    _REFRESH_TIMEOUT_SEC (the login attempt itself keeps running in its
    own thread regardless — see upstox_auto_login.py's own cleanup — this
    just stops the HTTP response from waiting on it).

    force=True on a request the USER just explicitly asked for is
    deliberate: unlike the scheduler's periodic safety net (which skips a
    still-valid token to avoid needless re-logins), a manual click means
    "I know something's wrong, log in again regardless."

    Returns {"success": bool, "configured": bool, ...current token_status()}.
    success=False with configured=False means auto-login isn't set up at
    all (UPSTOX_USERNAME/PIN/TOTP_SECRET missing) — the user needs the
    manual `python3 -m auth.upstox_auth` flow instead; this
    endpoint can't do anything for them in that case.
    """
    status = token_status()
    if not status["configured"]:
        return {"success": False, **status}

    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(ensure_fresh_upstox_token, True)
    try:
        new_token = future.result(timeout=_REFRESH_TIMEOUT_SEC)
    except FutureTimeoutError:
        log.warning("routes_upstox_token: manual refresh exceeded %ds — abandoning this request, login may still complete in the background.", _REFRESH_TIMEOUT_SEC)
        return {"success": False, **status, "timed_out": True}
    finally:
        executor.shutdown(wait=False)

    return {"success": new_token is not None, **token_status()}
