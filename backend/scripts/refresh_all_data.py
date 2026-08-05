"""
scripts/refresh_all_data.py — ONE command to keep everything current.

Before this script existed, keeping the data pipeline current meant a
human remembering to run 3+ separate scripts in the right order:
  1. utils.instrument_cache — refresh today's instrument master
  2. scripts/fill_bhavcopy_gap.py — extend MySQL's fno_bhavcopy table to today
  3. scripts/download_real_history.py — refresh live minute candles

This runs all of them, in order, idempotently (each step already skips
work that's not needed — cached-today files, dates already in the DB) —
safe to schedule via cron (e.g. daily before market open) or run by hand.

Usage:
    python3 scripts/refresh_all_data.py
    python3 scripts/refresh_all_data.py --symbols RELIANCE,TCS,NIFTY
    python3 scripts/refresh_all_data.py --skip-live   # bhavcopy DB only, no Upstox calls
"""
import argparse
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def run_step(name: str, cmd: list) -> bool:
    """Run one pipeline step as a subprocess. Returns True on success."""
    print(f"\n{'=' * 70}\n[refresh_all_data] {name}\n{'=' * 70}", file=sys.stderr)
    print(f"$ {' '.join(cmd)}", file=sys.stderr)
    result = subprocess.run(cmd, cwd=str(REPO_ROOT))
    if result.returncode != 0:
        print(f"[refresh_all_data] '{name}' exited {result.returncode} — continuing with remaining steps.", file=sys.stderr)
        return False
    return True


def refresh_instrument_master() -> bool:
    """Step 1: refresh today's Upstox instrument master and bulk seed to MySQL."""
    from db.engine import engine
    from utils.instrument_cache import InstrumentCache
    try:
        cache = InstrumentCache()
        # Sync all instruments (NSE, MCX, BSE) to the MySQL database
        cache.sync_to_db(engine)
        print("[refresh_all_data] Instrument master is current and synced to MySQL DB.", file=sys.stderr)
        return True
    except Exception as exc:
        print(f"[refresh_all_data] Could not refresh and sync instrument master: {exc}", file=sys.stderr)
        return False


def bhavcopy_gap_range() -> tuple:
    """
    Step 2 prep: find the last date already in MySQL fno_bhavcopy table and
    return (from_date, to_date) to fill — up to yesterday, since a
    pre-market cron run happens before today's bhavcopy is published.
    """
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    from sqlalchemy import text

    from db.engine import get_session
    try:
        with get_session() as session:
            row = session.execute(text("SELECT MAX(trade_date) FROM fno_bhavcopy")).fetchone()
            last_date = row[0] if row and row[0] else None
    except Exception as exc:
        print(f"[refresh_all_data] Error querying MySQL: {exc}", file=sys.stderr)
        return None, None

    if last_date is None:
        return "2020-09-01", yesterday
    from_date = (datetime.strptime(last_date, "%Y-%m-%d").date() + timedelta(days=1)).isoformat()
    if from_date > yesterday:
        return None, None  # already current
    return from_date, yesterday


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--symbols", default=None, help="Comma-separated symbols for live data refresh (default: config.TenPercentOTMStrangleConfig.SYMBOLS)")
    parser.add_argument("--skip-live", action="store_true", help="Skip live Upstox candle download (bhavcopy DB refresh only)")
    parser.add_argument("--days", type=int, default=5, help="Trailing days of live candles to (re)download")
    args = parser.parse_args()

    results = {}

    # Step 1: instrument master (needed by everything below)
    results["instrument_master"] = refresh_instrument_master()

    # Step 2: bhavcopy gap-fill, auto-detecting the range that's actually missing
    from_date, to_date = bhavcopy_gap_range()
    if from_date is None:
        print("[refresh_all_data] Bhavcopy DB is already current through yesterday — skipping gap-fill.", file=sys.stderr)
        results["bhavcopy_gap"] = True
    else:
        results["bhavcopy_gap"] = run_step(
            f"Filling bhavcopy gap {from_date} -> {to_date}",
            [sys.executable, "scripts/fill_bhavcopy_gap.py", "--from-date", from_date, "--to-date", to_date],
        )

    if not args.skip_live:
        from config import TenPercentOTMStrangleConfig
        symbols = args.symbols.split(",") if args.symbols else TenPercentOTMStrangleConfig.SYMBOLS
        symbols_arg = ",".join(s.strip().upper() for s in symbols)

        # Step 3: live minute candles for the configured trading universe
        results["live_candles"] = run_step(
            f"Refreshing live candles for {symbols_arg}",
            [sys.executable, "scripts/download_real_history.py", "--symbols", symbols_arg, "--days", str(args.days)],
        )
    else:
        print("[refresh_all_data] --skip-live given — not touching live Upstox data.", file=sys.stderr)

    print(f"\n{'=' * 70}\n[refresh_all_data] Summary\n{'=' * 70}", file=sys.stderr)
    for step, ok in results.items():
        print(f"  {'OK  ' if ok else 'FAIL'} — {step}", file=sys.stderr)

    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
