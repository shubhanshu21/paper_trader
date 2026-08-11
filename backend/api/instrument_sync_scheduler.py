"""
api/instrument_sync_scheduler.py — keeps the `instruments` MySQL table
(used by routes_terminal.py's search/autocomplete) in sync with the
CSV-based InstrumentCache the rest of the app already uses for real
order placement/lot-size/strike lookups.

Historically nothing ever wrote to this table after its one-time seed —
routes_terminal.py's search has been silently serving stale data
indefinitely (found via a real user report: Terminal search results
didn't match what InstrumentCache/live trading actually saw). This task
eagerly refreshes the CSV cache AND syncs it into the DB table once at
API startup, so the day's data is guaranteed ready before anyone logs
in rather than whichever request happens to need the CSV cache first
paying the download cost — then re-syncs once daily thereafter (same
"once a day" marker-file contract as token_refresh_scheduler.py; the
instrument master doesn't care about market hours the way token refresh
does, so this doesn't gate on assert_market_is_open()).
"""
import asyncio
import threading
from datetime import date
from pathlib import Path

from sqlalchemy import text

from db.engine import SessionLocal
from utils.instrument_cache import InstrumentCache
from utils.logger import get_logger
from utils.notify import notify

log = get_logger(__name__)

_CHECK_INTERVAL_SEC = 1800  # cheap marker-file check; the real work only happens once/day
_BATCH_SIZE = 5000
_instrument_cache = InstrumentCache()

# Guards _sync_once's DELETE-then-batch-insert against ever running twice
# at once — the module's own startup-then-loop sequencing already can't
# overlap with itself, but this is cheap insurance against a future caller
# (a manual re-sync endpoint, a second scheduler task) racing the wholesale
# table replace: two interleaved DELETE FROM instruments + re-insert passes
# would otherwise both hold row locks on the same table and serialize
# anyway, but non-deterministically re-wipe/re-insert each other's work.
_sync_lock = threading.Lock()

_INSERT_SQL = text(
    """
    INSERT INTO instruments
    (instrument_key, exchange_token, symbol, name, last_price, expiry, strike, tick_size, lot_size, instrument_type, option_type, exchange)
    VALUES
    (:instrument_key, :exchange_token, :symbol, :name, :last_price, :expiry, :strike, :tick_size, :lot_size, :instrument_type, :option_type, :exchange)
    """
)


def _marker_path() -> Path:
    return Path("logs") / f".instrument_sync_done_{date.today().isoformat()}"


def _clean(value) -> object | None:
    """InstrumentCache._load_csv() pre-fills every NaN with '' (for safe string ops elsewhere) — convert that back to real NULL for the DB columns that are genuinely optional (futures/equities have no strike/option_type, etc.)."""
    if value == "" or value is None:
        return None
    return value


def _sync_once() -> int:
    """
    Refreshes the CSV cache (a real network call only if today's file is
    missing — see InstrumentCache.get_or_refresh()) and replaces the
    instruments table wholesale — it's a full daily snapshot, not an
    incremental diff, matching how Upstox itself publishes it. DELETE
    (not TRUNCATE) so the whole replace stays inside one transaction —
    a failure partway through rolls back to yesterday's still-valid
    data instead of leaving the table empty.
    """
    df = _instrument_cache.get_or_refresh()

    rows = [
        {
            "instrument_key": record.get("instrument_key"),
            "exchange_token": _clean(record.get("exchange_token")),
            "symbol": record.get("symbol"),
            "name": _clean(record.get("name")),
            "last_price": _clean(record.get("last_price")),
            "expiry": _clean(record.get("expiry")),
            "strike": _clean(record.get("strike")),
            "tick_size": _clean(record.get("tick_size")),
            "lot_size": _clean(record.get("lot_size")),
            "instrument_type": _clean(record.get("instrument_type")),
            "option_type": _clean(record.get("option_type")),
            "exchange": record.get("exchange"),
        }
        for record in df.to_dict(orient="records")
    ]
    if not rows:
        return 0

    if not _sync_lock.acquire(blocking=False):
        log.warning("instrument_sync_scheduler: a sync is already in progress — skipping this call rather than racing it.")
        return 0
    try:
        db = SessionLocal()
        try:
            db.execute(text("DELETE FROM instruments"))
            for i in range(0, len(rows), _BATCH_SIZE):
                db.execute(_INSERT_SQL, rows[i:i + _BATCH_SIZE])
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
    finally:
        _sync_lock.release()
    return len(rows)


async def instrument_sync_scheduler() -> None:
    """Persistent background task — see module docstring. Never raises; logs and keeps ticking."""
    log.info("instrument_sync_scheduler: started.")
    try:
        count = await asyncio.to_thread(_sync_once)
        log.info("instrument_sync_scheduler: startup sync wrote %d rows.", count)
        _marker_path().parent.mkdir(parents=True, exist_ok=True)
        _marker_path().touch()
    except Exception as exc:
        log.error("instrument_sync_scheduler: startup sync failed: %s", exc, exc_info=True)
        notify(
            "instrument_sync",
            f"Startup instrument database sync failed: {exc}. Terminal search may show stale data until the next retry.",
            level="warning",
        )

    while True:
        await asyncio.sleep(_CHECK_INTERVAL_SEC)
        try:
            if _marker_path().exists():
                continue
            count = await asyncio.to_thread(_sync_once)
            log.info("instrument_sync_scheduler: daily sync wrote %d rows.", count)
            _marker_path().parent.mkdir(parents=True, exist_ok=True)
            _marker_path().touch()
        except Exception as exc:
            log.error("instrument_sync_scheduler: tick failed: %s", exc, exc_info=True)
