"""
api/token_refresh_scheduler.py — daily Upstox token auto-login, running as
a background asyncio task inside the API process.

This used to be run_daemon.py's job (it called ensure_fresh_upstox_token()
once at startup and once per market-open loop tick). Now that run_daemon.py
itself is retired (it only ever ran hand-written strategies — see
strategies/registry.py — which have been replaced by the custom-strategy
builder), token refresh needed a new home so daily auto-login doesn't
silently stop working. Same "once a day, market hours only" contract as
before, same bounded-timeout wrapper (a stuck Selenium session must never
block anything else in this process — see market_price_broadcaster.py/
custom_strategy_scheduler.py, which both need a working token too).
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import date
from pathlib import Path
from typing import Optional

from automate.auth.upstox_auto_login import ensure_fresh_upstox_token
from automate.compliance.sebi_rules import assert_market_is_open
from automate.utils.logger import get_logger
from automate.utils.notify import notify

log = get_logger(__name__)

_CHECK_INTERVAL_SEC = 300
_TOKEN_REFRESH_TIMEOUT_SEC = 300


def _marker_path() -> Path:
    return Path("logs") / f".api_token_refresh_done_{date.today().isoformat()}"


def _market_is_open_now() -> bool:
    try:
        assert_market_is_open()
        return True
    except RuntimeError:
        return False


def _refresh_bounded() -> Optional[str]:
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(ensure_fresh_upstox_token)
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


async def token_refresh_scheduler() -> None:
    log.info("token_refresh_scheduler: started.")
    # Refresh once immediately at boot too — no-ops if UPSTOX_USERNAME/PIN/
    # TOTP_SECRET aren't set (auto-login opt-in), same as run_daemon.py
    # used to do before building any broker client.
    try:
        await asyncio.to_thread(_refresh_bounded)
    except Exception:
        log.exception("token_refresh_scheduler: startup refresh failed.")

    while True:
        await asyncio.sleep(_CHECK_INTERVAL_SEC)
        try:
            if not _market_is_open_now():
                continue
            marker = _marker_path()
            if marker.exists():
                continue
            await asyncio.to_thread(_refresh_bounded)
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
        except Exception:
            log.exception("token_refresh_scheduler: tick failed.")
