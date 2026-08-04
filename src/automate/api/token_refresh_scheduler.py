"""
api/token_refresh_scheduler.py — Upstox token auto-login/auto-heal, running
as a background asyncio task inside the API process.

This used to be run_daemon.py's job (it called ensure_fresh_upstox_token()
once at startup and once per market-open loop tick). Now that run_daemon.py
itself is retired (it only ever ran hand-written strategies — see
strategies/registry.py — which have been replaced by the custom-strategy
builder), token refresh needed a new home so daily auto-login doesn't
silently stop working. Same bounded-timeout wrapper (a stuck Selenium
session must never block anything else in this process — see
market_price_broadcaster.py/custom_strategy_scheduler.py, which both need
a working token too).

Three things wake this loop up, all funnelled through the same
_MIN_REFRESH_GAP_SEC anti-hammer floor before any of them are allowed to
trigger a real attempt:

1. Periodic safety net (_CHECK_INTERVAL_SEC) — re-validates even when
   nothing else has flagged a problem. Upstox tokens have been observed
   going invalid mid-session well before their nominal daily expiry (e.g.
   Cloudflare bot-mitigation on the Selenium-driven login, or account-level
   single-session enforcement when a login happens elsewhere), and
   ensure_fresh_upstox_token() is cheap when the token is still valid — one
   real market-quote LTP check (upstox_auto_login._token_is_valid) and
   return, only paying for a full Selenium re-login when that check
   actually fails. An earlier version of this scheduler gated re-checks
   behind a "done today" marker file, which meant any invalidation after
   the first tick of the day went undetected — and every LTP/order call
   kept 401ing — until the process was manually restarted. Do not
   reintroduce a once-a-day gate here.

2. Reactive wake (upstox_broker.token_invalid_event) — set whenever a live
   LTP call's 401 circuit breaker trips (see broker/upstox_broker.py), so a
   dead token gets a real re-login attempt within seconds instead of
   waiting out the full _CHECK_INTERVAL_SEC. The circuit breaker itself
   only suppresses request volume, it never acquires a new token.

3. Proactive pre-expiry wake — Upstox access tokens are JWTs with a real
   `exp` claim (see upstox_auto_login.token_expiry_epoch), fixed to 3:30 AM
   IST regardless of issue time. Once we know that deadline exactly, we
   don't have to wait for it to actually fail first: schedule a forced
   refresh _PROACTIVE_LEAD_SEC before it expires. This one must pass
   force=True — at that point the token is still technically valid, so a
   normal validity-checked refresh would just report "still valid" and
   skip, defeating the entire point of refreshing ahead of time.

_MIN_REFRESH_GAP_SEC is the actual anti-hammer guarantee: no matter which
of the three paths above fires, or how close together they land, a real
attempt (get_ltp validity check, or a full Selenium login) can only happen
once per _MIN_REFRESH_GAP_SEC. A login that's genuinely failing repeatedly
just sits behind the circuit breaker (see upstox_broker.py) between
attempts instead of being retried back-to-back.
"""
import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError

from automate.auth.upstox_auto_login import ensure_fresh_upstox_token, token_expiry_epoch
from automate.broker.upstox_broker import token_invalid_event
from automate.config import UpstoxConfig
from automate.utils.logger import get_logger
from automate.utils.notify import notify

log = get_logger(__name__)

_CHECK_INTERVAL_SEC = 600
_TOKEN_REFRESH_TIMEOUT_SEC = 300
_MIN_REFRESH_GAP_SEC = 900
_PROACTIVE_LEAD_SEC = 600  # re-login 10 min before the token's own exp claim


def _refresh_bounded(force: bool = False) -> str | None:
    """
    Same contract as ensure_fresh_upstox_token(), but never blocks the
    caller past _TOKEN_REFRESH_TIMEOUT_SEC. The underlying Selenium call
    keeps running in its background thread either way (it cleans up its
    own browser session in a `finally`, see upstox_auto_login.py) — this
    just stops WAITING on it and moves on, so a slow/stuck login attempt
    can never freeze the scheduler loop for everyone else. Same pattern
    as cli/run_daemon.py's _ensure_fresh_upstox_token_bounded().
    """
    # NOT a `with` block deliberately — ThreadPoolExecutor's context-manager
    # exit calls shutdown(wait=True), which would block on the very thread
    # we're trying to stop waiting on. shutdown(wait=False) here lets that
    # thread keep running and clean up on its own, without us waiting on it.
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(ensure_fresh_upstox_token, force)
    try:
        return future.result(timeout=_TOKEN_REFRESH_TIMEOUT_SEC)
    except FutureTimeoutError:
        log.critical("Upstox token refresh exceeded %ds — abandoning this attempt, keeping existing token.", _TOKEN_REFRESH_TIMEOUT_SEC)
        notify(
            "upstox_login",
            f"Token refresh took longer than {_TOKEN_REFRESH_TIMEOUT_SEC}s and was abandoned — keeping the existing "
            f"token. If it has actually expired, entries will start failing until it's refreshed manually.",
        )
        return None
    finally:
        executor.shutdown(wait=False)


def _seconds_until_proactive_relogin() -> float:
    """
    Time left until _PROACTIVE_LEAD_SEC before the current token's own
    `exp` claim. Falls back to _CHECK_INTERVAL_SEC (i.e. "nothing special
    to schedule, just rely on the periodic safety net") if the token is
    missing or isn't a parseable JWT.
    """
    exp = token_expiry_epoch(UpstoxConfig.ACCESS_TOKEN)
    if exp is None:
        return _CHECK_INTERVAL_SEC
    return max(1.0, exp - time.time() - _PROACTIVE_LEAD_SEC)


async def token_refresh_scheduler() -> None:
    log.info("token_refresh_scheduler: started.")
    # Refresh once immediately at boot too — no-ops if UPSTOX_USERNAME/PIN/
    # TOTP_SECRET aren't set (auto-login opt-in), same as run_daemon.py
    # used to do before building any broker client.
    try:
        await asyncio.to_thread(_refresh_bounded)
    except Exception:
        log.exception("token_refresh_scheduler: startup refresh failed.")
    last_attempt = time.monotonic()

    while True:
        proactive_delay = _seconds_until_proactive_relogin()
        wait_for = min(_CHECK_INTERVAL_SEC, proactive_delay)
        woke_early = await asyncio.to_thread(token_invalid_event.wait, wait_for)
        token_invalid_event.clear()

        if time.monotonic() - last_attempt < _MIN_REFRESH_GAP_SEC:
            # A real attempt (of any kind, from any of the three triggers)
            # already ran too recently — never hammer Upstox's login flow,
            # regardless of why we just woke up.
            continue

        try:
            if woke_early:
                log.info("token_refresh_scheduler: woken early by circuit-breaker trip.")
                await asyncio.to_thread(_refresh_bounded)
            elif proactive_delay <= wait_for:
                # Hit the pre-expiry deadline computed from the JWT's own
                # `exp` claim, not a failure — the token is still valid
                # right now, so a normal validity-checked refresh would
                # just report "still valid" and skip. Force it so we
                # always rotate onto a new token before this one actually
                # expires, rather than waiting to react to a 401.
                log.info("token_refresh_scheduler: proactively refreshing ~%ds before token expiry.", _PROACTIVE_LEAD_SEC)
                await asyncio.to_thread(_refresh_bounded, True)
            else:
                # Periodic safety net — re-validate even though nothing
                # flagged a problem (see module docstring, point 1).
                await asyncio.to_thread(_refresh_bounded)
        except Exception:
            log.exception("token_refresh_scheduler: tick failed.")
        finally:
            last_attempt = time.monotonic()
