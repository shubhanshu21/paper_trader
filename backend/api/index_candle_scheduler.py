"""
api/index_candle_scheduler.py — keeps the MySQL index_1min_candles table
(backtest/synthetic_data_feed.py's underlying-candle source) current
without anyone having to remember to re-run scripts/import_kaggle_index_
candles.py by hand.

That import script did a ONE-TIME historical backfill from static Kaggle
files (NIFTY/BANKNIFTY 1-min OHLC) — those files stop at a fixed date
(2026-04-23) and nobody re-uploads them, so the table would silently go
stale forever without something re-filling the gap from a live source.
Same "found via the table silently going stale, same fix shape" story
api/bhavcopy_scheduler.py's own docstring describes for fno_bhavcopy —
and the same "once a day, gated by a marker file" contract.

The live source here is the broker itself (Upstox, via the same shared
paper/live broker pair custom_strategy_scheduler.py builds) —
get_historical_candles(unit="minutes", interval=1, ...) for NIFTY/
BANKNIFTY, real market data, not synthetic. The one real constraint:
Upstox's own live-history retention is ~30 days (see backtest/
__main__.py's LIVE_DATA_MAX_DAYS), so this can only ever bridge a gap up
to about that wide. A table that's gone stale for LONGER than that
(e.g. this app being down for months) has a permanent hole between
"30 days ago" and "whenever this last ran" that only another bulk
historical source (a fresh Kaggle-style download, or a paid vendor) can
fill — this scheduler fills what it honestly can and logs the rest as a
gap, exactly like bhavcopy_scheduler.py does for a bhavcopy day that
genuinely isn't published yet.
"""
import asyncio
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import text

from db.engine import SessionLocal
from scripts.import_kaggle_index_candles import INSERT_SQL, _to_float, _to_int
from utils.logger import get_logger
from utils.notify import notify

log = get_logger(__name__)

_CHECK_INTERVAL_SEC = 1800  # cheap marker-file check; the real work only happens once/day
# Run after market close so a full day's real 1-minute candles actually
# exist to fetch — same reasoning as bhavcopy_scheduler.py's _RUN_AFTER_IST.
_RUN_AFTER_IST = dtime(16, 0)
_IST = ZoneInfo("Asia/Kolkata")
_LIVE_HISTORY_MAX_DAYS = 30  # Upstox's own real retention limit for 1-min candles (verified empirically — see backtest/__main__.py)
_SYMBOLS = ("NIFTY", "BANKNIFTY")


def _marker_path() -> Path:
    return Path("logs") / f".index_candle_sync_done_{date.today().isoformat()}"


def _latest_ts(session, symbol: str) -> datetime | None:
    row = session.execute(text("SELECT MAX(ts) FROM index_1min_candles WHERE symbol = :s"), {"s": symbol}).fetchone()
    return row[0] if row and row[0] else None


def _sync_symbol(session, broker, symbol: str) -> dict:
    latest = _latest_ts(session, symbol)
    today = date.today()
    earliest_fetchable = today - timedelta(days=_LIVE_HISTORY_MAX_DAYS)

    gap_start = (latest.date() + timedelta(days=1)) if latest else earliest_fetchable
    # Whatever's older than Upstox's own retention window is permanently
    # out of reach for THIS scheduler — reported, not silently dropped.
    permanently_missed_days = max(0, (earliest_fetchable - gap_start).days)
    fetch_start = max(gap_start, earliest_fetchable)

    if fetch_start > today:
        return {"symbol": symbol, "inserted": 0, "gap_unfillable_days": permanently_missed_days, "status": "already_current"}

    instrument_key = broker.resolve_instrument_key(symbol)
    raw = broker.get_historical_candles(instrument_key, "minutes", 1, today.isoformat())
    if not raw:
        return {"symbol": symbol, "inserted": 0, "gap_unfillable_days": permanently_missed_days, "status": "no_data"}

    batch = []
    for row in raw:
        try:
            ts = datetime.fromisoformat(row["timestamp"]).replace(tzinfo=None)
        except (KeyError, ValueError):
            continue
        if ts.date() < fetch_start:
            continue
        o, h, lo, c = _to_float(row.get("open")), _to_float(row.get("high")), _to_float(row.get("low")), _to_float(row.get("close"))
        if None in (o, h, lo, c):
            continue
        batch.append({
            "symbol": symbol, "ts": ts, "open": o, "high": h, "low": lo, "close": c,
            "volume": _to_int(row.get("volume")), "oi": None, "source": "upstox_live",
        })

    if batch:
        # SessionLocal (db/engine.py) auto-begins a transaction on first
        # use — an explicit session.begin() here double-begins and raises.
        session.execute(INSERT_SQL, batch)
        session.commit()

    return {"symbol": symbol, "inserted": len(batch), "gap_unfillable_days": permanently_missed_days, "status": "synced"}


def _sync_all() -> list[dict]:
    """Runs inside asyncio.to_thread — plain blocking DB/broker calls, no event loop of its own."""
    from api.custom_strategy_scheduler import _get_brokers

    brokers = _get_brokers()
    if not brokers:
        raise RuntimeError("brokers not ready yet")
    broker = brokers.get("paper") or brokers.get("live")
    if broker is None:
        raise RuntimeError("no broker available")

    results = []
    session = SessionLocal()
    try:
        for symbol in _SYMBOLS:
            results.append(_sync_symbol(session, broker, symbol))
    finally:
        session.close()
    return results


async def index_candle_scheduler() -> None:
    """Persistent background task — see module docstring. Never raises; logs and keeps ticking."""
    log.info("index_candle_scheduler: started.")
    while True:
        try:
            if datetime.now(_IST).time() >= _RUN_AFTER_IST and not _marker_path().exists():
                results = await asyncio.to_thread(_sync_all)
                for r in results:
                    log.info(
                        "index_candle_scheduler: %s -> %s | inserted=%d unfillable_gap_days=%d",
                        r["symbol"], r["status"], r["inserted"], r["gap_unfillable_days"],
                    )
                    if r["gap_unfillable_days"] > 0:
                        notify(
                            "index_candle_scheduler",
                            f"index_1min_candles for {r['symbol']} has a {r['gap_unfillable_days']}-day gap older than "
                            f"Upstox's ~{_LIVE_HISTORY_MAX_DAYS}-day live history retention — this scheduler can't back-fill "
                            f"it. A fresh bulk historical download is the only way to close it.",
                            level="warning",
                        )
                _marker_path().parent.mkdir(parents=True, exist_ok=True)
                _marker_path().touch()
        except Exception as exc:
            log.error("index_candle_scheduler: tick failed: %s", exc, exc_info=True)
        await asyncio.sleep(_CHECK_INTERVAL_SEC)
