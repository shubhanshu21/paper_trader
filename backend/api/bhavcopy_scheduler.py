"""
api/bhavcopy_scheduler.py — keeps the MySQL fno_bhavcopy table (the
backtest engine's BhavcopyDataFeed) current without anyone having to
remember to run scripts/fill_bhavcopy_gap.py by hand.

Found via a real user report: nothing in this app ever scheduled that
script (no cron entry, no background task) — the table had silently gone
10 days stale before anyone noticed, since none of its consumers fail
loudly on stale data, they just quietly serve an older "most recent
date" than today.

Same "once a day, gated by a marker file" contract as
instrument_sync_scheduler.py/token_refresh_scheduler.py — reuses
scripts/fill_bhavcopy_gap.py's own import_day()/already_have() directly
(no separate reimplementation) to fill every trading day from the last
date already in the DB through today. NSE typically doesn't publish a
given day's bhavcopy until the evening, so this runs late (after
_RUN_AFTER_IST) rather than right after market close — a day whose
bhavcopy genuinely isn't out yet just comes back "no_data" and gets
picked up automatically on the NEXT day's run (import_day is idempotent,
so there's no gap to permanently miss), never a hard failure.
"""
import asyncio
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from sqlalchemy import text

from db.engine import get_session
from scripts.fill_bhavcopy_gap import NSE_HEADERS, import_day
from utils.logger import get_logger
from utils.notify import notify

log = get_logger(__name__)

_CHECK_INTERVAL_SEC = 1800  # cheap marker-file check; the real work only happens once/day
# NSE's own bhavcopy publish time varies (usually evening, sometimes not
# until after 20:00 IST) — running the daily attempt this late maximizes
# the chance THIS run actually picks up today's data instead of leaving
# it for tomorrow's run.
_RUN_AFTER_IST = dtime(20, 30)
_IST = ZoneInfo("Asia/Kolkata")
_DELAY_BETWEEN_REQUESTS_SEC = 0.4


def _marker_path() -> Path:
    return Path("logs") / f".bhavcopy_sync_done_{date.today().isoformat()}"


def _latest_trade_date(session) -> date | None:
    """fno_bhavcopy.trade_date is stored as a VARCHAR (ISO string, see import_day()'s own row dict) — MAX() over it returns a str, not a date, so it needs parsing back before any date arithmetic."""
    row = session.execute(text("SELECT MAX(trade_date) FROM fno_bhavcopy")).fetchone()
    return date.fromisoformat(row[0]) if row and row[0] else None


def _fill_gap_to_today() -> dict:
    """Imports every trading day from (latest date in DB + 1) through today. Runs inside asyncio.to_thread — a plain blocking function, no event loop of its own. Returns a summary dict for logging."""
    import contextlib
    import time as time_module

    http_session = requests.Session()
    http_session.headers.update(NSE_HEADERS)
    with contextlib.suppress(requests.exceptions.RequestException):
        http_session.get("https://www.nseindia.com", timeout=15)  # warm-up for cookies, same as the script

    imported = no_data = skipped = 0
    with get_session() as session:
        latest = _latest_trade_date(session)
        # No data at all yet (fresh DB) — same 180-day depth the rest of
        # this app's "available dates" lists already cap at (routes_
        # chain_replay.py's _MAX_DATES), rather than trying to backfill
        # this table's entire multi-year history on one cold start.
        start = (latest + timedelta(days=1)) if latest else (date.today() - timedelta(days=180))
        today = date.today()
        d = start
        while d <= today:
            if d.weekday() < 5:
                status = import_day(session, http_session, d)
                if status == "imported":
                    imported += 1
                elif status == "no_data":
                    no_data += 1
                else:
                    skipped += 1
                time_module.sleep(_DELAY_BETWEEN_REQUESTS_SEC)
            d += timedelta(days=1)
    return {"imported": imported, "no_data": no_data, "skipped": skipped, "from": start.isoformat(), "to": today.isoformat()}


async def bhavcopy_scheduler() -> None:
    """Persistent background task — see module docstring. Never raises; logs and keeps ticking."""
    log.info("bhavcopy_scheduler: started.")
    while True:
        try:
            if datetime.now(_IST).time() >= _RUN_AFTER_IST and not _marker_path().exists():
                summary = await asyncio.to_thread(_fill_gap_to_today)
                log.info(
                    "bhavcopy_scheduler: gap-fill %s→%s | imported=%d no_data=%d skipped(existing)=%d",
                    summary["from"], summary["to"], summary["imported"], summary["no_data"], summary["skipped"],
                )
                _marker_path().parent.mkdir(parents=True, exist_ok=True)
                _marker_path().touch()
        except Exception as exc:
            log.error("bhavcopy_scheduler: tick failed: %s", exc, exc_info=True)
            notify(
                "bhavcopy_scheduler",
                f"Daily bhavcopy sync failed: {exc}. OI Scanner/Chain Replay/Simulator EOD Replay data may fall behind until the next retry.",
                level="warning",
            )
        await asyncio.sleep(_CHECK_INTERVAL_SEC)
