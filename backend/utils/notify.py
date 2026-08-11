"""
utils/notify.py — the single funnel for "the user needs to know about this
right now" events: writes an in-app Notification row (surfaced via the Bell
icon in the UI — GET /api/notifications, pushed live over
/ws/notifications) AND sends the same message to Telegram
(utils/telegram_alert.alert_error) if configured.

Deliberately deduplicated in-process: a background scheduler ticking
every 60s that hits the same persistent failure (Upstox token expired,
one leg stuck failing to fill, instrument master download broken) must
not write a new DB row and fire a new Telegram message every single tick
forever — that's not a notification system, it's a spam generator nobody
will read past the first one. The identical (source, message) pair is
suppressed for _DEDUP_WINDOW_SEC after it first fires; a genuinely new
message (even from the same source) still goes through immediately.

Like telegram_alert.send_telegram_alert(), this NEVER raises — a
notification failure must never be able to break whatever background
task is reporting through it.
"""
import re
import threading
import time

from utils.logger import get_logger
from utils.telegram_alert import alert_error

log = get_logger(__name__)

_DEDUP_WINDOW_SEC = 900  # 15 minutes — long enough to kill tick-by-tick spam, short enough that a same-day recurrence after a fix still gets reported.
_recent_lock = threading.Lock()
_recent: dict[tuple[int | None, str, str], float] = {}

# Matches any run of digits (with an optional decimal point/comma group
# inside it, e.g. "12,345.67") — used only to build the dedup key below.
_NUMBER_RE = re.compile(r"[0-9][0-9,]*(?:\.[0-9]+)?")


def _dedup_key_message(message: str) -> str:
    """
    Collapse every number in `message` down to a single placeholder before
    it's used as (part of) the dedup key.

    A retry-every-tick failure (e.g. a margin shortfall notify() call that
    embeds the leg's CURRENT required-margin/available-balance figures)
    would otherwise produce a technically-unique message on every single
    call — those numbers move a little each tick — sailing straight past
    the window below and defeating the dedup entirely. The stored/sent
    message itself is untouched; only the key used to decide "have we
    already said this" is normalized.
    """
    return _NUMBER_RE.sub("#", message)


def notify(source: str, message: str, level: str = "error", user_id: int | None = None) -> None:
    """
    Args:
        source: short identifying tag (e.g. "custom_strategy_scheduler",
            "token_refresh", "instrument_master") — shown in the UI/Telegram
            so the user knows which subsystem is talking.
        message: human-readable description of what happened.
        level: "info" | "warning" | "error". Telegram is only used for
            warning/error — routine info-level notifications stay in-app only.
        user_id: owning CustomStrategy's user_id, so only that account sees
            this in-app (routes_notifications.py filters by it). Leave None
            for system-wide events with no single owner (broker login,
            instrument master download) — those are admin-only, not
            per-user. Telegram delivery is unaffected either way (there's
            one shared Telegram chat for the whole panel).
    """
    key = (user_id, source, _dedup_key_message(message))
    now = time.monotonic()
    with _recent_lock:
        last = _recent.get(key)
        if last is not None and now - last < _DEDUP_WINDOW_SEC:
            return
        _recent[key] = now
        # Bound memory for a long-running process — an unbounded dict of
        # every distinct message ever seen would slowly leak.
        if len(_recent) > 500:
            cutoff = now - _DEDUP_WINDOW_SEC
            for k in [k for k, ts in _recent.items() if ts < cutoff]:
                del _recent[k]

    try:
        from db.engine import SessionLocal
        from db.models import Notification

        db = SessionLocal()
        try:
            db.add(Notification(level=level, source=source, message=message, user_id=user_id))
            db.commit()
        finally:
            db.close()
    except Exception:
        log.exception("notify(): failed to write notification row for source=%s", source)

    if level in ("error", "warning"):
        try:
            alert_error(source, message)
        except Exception:
            log.exception("notify(): Telegram alert failed for source=%s", source)
