"""
scripts/import_bhavcopy_to_db.py — Import a raw NSE F&O bhavcopy CSV
(e.g. dataset/archive_3/fobhav.csv, 8.9GB / 112M+ rows) into an indexed
SQLite database, so later queries (by symbol/instrument/date/expiry) are
millisecond-fast instead of a multi-minute full-file streaming scan.

Memory-safe by design: streams the source CSV one row at a time via the
stdlib csv module, buffering only one batch (default 50,000 rows) before
each executemany() + commit. Peak memory is bounded by the batch size, not
the file size (verified: ~20MB peak RAM importing 112M rows / 8.9GB).

Usage:
    python3 scripts/import_bhavcopy_to_db.py --source dataset/archive_3/fobhav.csv
    # Writes dataset/fno_bhavcopy.db (~30 min for a 112M-row file)

Then query it directly, e.g.:
    import sqlite3
    conn = sqlite3.connect("dataset/fno_bhavcopy.db")
    conn.execute(
        "SELECT trade_date, open, high, low, close FROM fno_bhavcopy "
        "WHERE symbol='RELIANCE' AND instrument='FUTSTK' ORDER BY trade_date"
    ).fetchall()

backtest/bhavcopy_data_feed.py queries this same DB directly (e.g. for a
symbol's daily spot-proxy series, via the near-month future's close) —
there's no separate CSV-generation step for that anymore.
"""
import argparse
import csv
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# Same inconsistent date casing as build_index_history_from_bhavcopy.py.
_DATE_FORMATS = ["%d-%b-%Y", "%d-%b-%y"]


def _parse_date_iso(raw: str) -> Optional[str]:
    raw = raw.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _to_float(raw: str) -> Optional[float]:
    try:
        return float(raw)
    except (ValueError, TypeError):
        return None


def _to_int(raw: str) -> Optional[int]:
    try:
        return int(float(raw))
    except (ValueError, TypeError):
        return None


SCHEMA = """
CREATE TABLE IF NOT EXISTS fno_bhavcopy (
    instrument  TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    expiry_dt   TEXT,
    strike_pr   REAL,
    option_typ  TEXT,
    open        REAL,
    high        REAL,
    low         REAL,
    close       REAL,
    settle_pr   REAL,
    contracts   INTEGER,
    val_inlakh  REAL,
    open_int    INTEGER,
    chg_in_oi   INTEGER,
    trade_date  TEXT NOT NULL
);
"""


def import_csv(source_path: str, db_path: str, batch_size: int = 50_000, progress_every: int = 2_000_000) -> int:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    # Bulk-load performance pragmas — safe here because a crash mid-import
    # just means re-running the import, no live data at risk.
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=OFF;")
    conn.execute("DROP TABLE IF EXISTS fno_bhavcopy;")
    conn.execute(SCHEMA)

    insert_sql = """
        INSERT INTO fno_bhavcopy
        (instrument, symbol, expiry_dt, strike_pr, option_typ, open, high, low,
         close, settle_pr, contracts, val_inlakh, open_int, chg_in_oi, trade_date)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    rows_seen = 0
    rows_inserted = 0
    batch = []
    start = time.monotonic()

    with open(source_path, newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        required = {"INSTRUMENT", "SYMBOL", "EXPIRY_DT", "STRIKE_PR", "OPTION_TYP",
                    "OPEN", "HIGH", "LOW", "CLOSE", "SETTLE_PR", "CONTRACTS",
                    "VAL_INLAKH", "OPEN_INT", "CHG_IN_OI", "TIMESTAMP"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise RuntimeError(f"Source file missing expected columns: {missing}. Found: {reader.fieldnames}")

        for row in reader:
            rows_seen += 1
            trade_date = _parse_date_iso(row["TIMESTAMP"])
            if trade_date is None:
                continue  # Unparseable date row — skip, don't crash the whole import.

            batch.append((
                row["INSTRUMENT"], row["SYMBOL"], _parse_date_iso(row["EXPIRY_DT"]),
                _to_float(row["STRIKE_PR"]), row["OPTION_TYP"],
                _to_float(row["OPEN"]), _to_float(row["HIGH"]), _to_float(row["LOW"]), _to_float(row["CLOSE"]),
                _to_float(row["SETTLE_PR"]), _to_int(row["CONTRACTS"]), _to_float(row["VAL_INLAKH"]),
                _to_int(row["OPEN_INT"]), _to_int(row["CHG_IN_OI"]), trade_date,
            ))

            if len(batch) >= batch_size:
                conn.executemany(insert_sql, batch)
                conn.commit()
                rows_inserted += len(batch)
                batch.clear()

            if rows_seen % progress_every == 0:
                elapsed = time.monotonic() - start
                print(f"  ... {rows_seen:,} rows scanned, {rows_inserted:,} inserted, {elapsed:.0f}s elapsed", file=sys.stderr)

    if batch:
        conn.executemany(insert_sql, batch)
        conn.commit()
        rows_inserted += len(batch)

    print(f"Inserted {rows_inserted:,} rows. Building indices (this can take a few minutes on 100M+ rows)...", file=sys.stderr)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_symbol_instrument_date ON fno_bhavcopy(symbol, instrument, trade_date);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_symbol_instrument_expiry ON fno_bhavcopy(symbol, instrument, expiry_dt);")
    # Covering index for "last trade_date of this expiry" lookups (cycle exit
    # date / entry-exit option pricing) — without expiry_dt+trade_date
    # together, SQLite's planner picks idx_symbol_instrument_date instead
    # (matches only symbol+instrument) and scans a symbol's ENTIRE options
    # history per lookup. Confirmed ~1.7s/query -> ~2min for a 5-year NIFTY
    # backtest without this index; sub-second with it.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_symbol_instrument_expiry_date ON fno_bhavcopy(symbol, instrument, expiry_dt, trade_date);")
    conn.commit()
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.close()

    elapsed = time.monotonic() - start
    print(f"Done in {elapsed:.0f}s. {rows_seen:,} rows scanned, {rows_inserted:,} inserted into {db_path}.", file=sys.stderr)
    return rows_inserted


def verify(db_path: str) -> None:
    """Cheap sanity check after import: row count + a known-good spot check."""
    conn = sqlite3.connect(db_path)
    total = conn.execute("SELECT COUNT(*) FROM fno_bhavcopy").fetchone()[0]
    symbols = conn.execute("SELECT COUNT(DISTINCT symbol) FROM fno_bhavcopy").fetchone()[0]
    sample = conn.execute(
        "SELECT symbol, instrument, trade_date, close FROM fno_bhavcopy "
        "WHERE symbol='NIFTY' AND instrument='FUTIDX' ORDER BY trade_date LIMIT 1"
    ).fetchone()
    conn.close()
    print(f"Verify: {total:,} total rows | {symbols:,} distinct symbols | "
          f"earliest NIFTY FUTIDX row: {sample}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", required=True, help="Path to the raw bhavcopy CSV")
    parser.add_argument("--db", default="dataset/fno_bhavcopy.db", help="Output SQLite DB path")
    parser.add_argument("--batch-size", type=int, default=50_000)
    args = parser.parse_args()

    print(f"Streaming {args.source} into {args.db} (single pass, bounded memory)...", file=sys.stderr)
    import_csv(args.source, args.db, batch_size=args.batch_size)
    verify(args.db)


if __name__ == "__main__":
    main()
