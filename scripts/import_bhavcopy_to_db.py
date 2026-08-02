"""
scripts/import_bhavcopy_to_db.py — Import a raw NSE F&O bhavcopy CSV
(e.g. dataset/archive_3/fobhav.csv, 8.9GB / 112M+ rows) into the MySQL
`fno_bhavcopy` table (see db/models.py::FnoBhavcopy), so later queries
(by symbol/instrument/date/expiry) are millisecond-fast instead of a
multi-minute full-file streaming scan.

Memory-safe by design: streams the source CSV one row at a time via the
stdlib csv module, buffering only one batch (default 50,000 rows) before
each executemany() + commit. Peak memory is bounded by the batch size,
not the file size.

Usage:
    python3 scripts/import_bhavcopy_to_db.py --source dataset/archive_3/fobhav.csv
    # Bulk-loads into the fno_bhavcopy table (~30 min for a 112M-row file)

backtest/bhavcopy_data_feed.py and backtest/historical_engine.py query
this same MySQL table directly (via SessionLocal/text()) — there's no
separate CSV-generation step for that anymore.
"""
import argparse
import csv
import sys
import time
from datetime import datetime
from typing import Optional

from sqlalchemy import text

from automate.db.engine import engine as _default_engine

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


INSERT_SQL = text(
    """
    INSERT INTO fno_bhavcopy
    (instrument, symbol, expiry_dt, strike_pr, option_typ, open, high, low,
     close, settle_pr, contracts, val_inlakh, open_int, chg_in_oi, trade_date)
    VALUES
    (:instrument, :symbol, :expiry_dt, :strike_pr, :option_typ, :open, :high, :low,
     :close, :settle_pr, :contracts, :val_inlakh, :open_int, :chg_in_oi, :trade_date)
    """
)


def import_csv(source_path: str, engine=None, batch_size: int = 50_000, progress_every: int = 2_000_000,
                truncate_first: bool = False) -> int:
    engine = engine or _default_engine

    rows_seen = 0
    rows_inserted = 0
    batch = []
    start = time.monotonic()

    with engine.begin() as conn:
        if truncate_first:
            print("Truncating existing fno_bhavcopy rows before import...", file=sys.stderr)
            conn.execute(text("TRUNCATE TABLE fno_bhavcopy"))

    with open(source_path, newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        required = {"INSTRUMENT", "SYMBOL", "EXPIRY_DT", "STRIKE_PR", "OPTION_TYP",
                    "OPEN", "HIGH", "LOW", "CLOSE", "SETTLE_PR", "CONTRACTS",
                    "VAL_INLAKH", "OPEN_INT", "CHG_IN_OI", "TIMESTAMP"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise RuntimeError(f"Source file missing expected columns: {missing}. Found: {reader.fieldnames}")

        conn = engine.connect()
        try:
            for row in reader:
                rows_seen += 1
                trade_date = _parse_date_iso(row["TIMESTAMP"])
                if trade_date is None:
                    continue  # Unparseable date row — skip, don't crash the whole import.

                batch.append({
                    "instrument": row["INSTRUMENT"], "symbol": row["SYMBOL"],
                    "expiry_dt": _parse_date_iso(row["EXPIRY_DT"]),
                    "strike_pr": _to_float(row["STRIKE_PR"]), "option_typ": row["OPTION_TYP"],
                    "open": _to_float(row["OPEN"]), "high": _to_float(row["HIGH"]), "low": _to_float(row["LOW"]),
                    "close": _to_float(row["CLOSE"]), "settle_pr": _to_float(row["SETTLE_PR"]),
                    "contracts": _to_int(row["CONTRACTS"]), "val_inlakh": _to_float(row["VAL_INLAKH"]),
                    "open_int": _to_int(row["OPEN_INT"]), "chg_in_oi": _to_int(row["CHG_IN_OI"]),
                    "trade_date": trade_date,
                })

                if len(batch) >= batch_size:
                    with conn.begin():
                        conn.execute(INSERT_SQL, batch)
                    rows_inserted += len(batch)
                    batch.clear()

                if rows_seen % progress_every == 0:
                    elapsed = time.monotonic() - start
                    print(f"  ... {rows_seen:,} rows scanned, {rows_inserted:,} inserted, {elapsed:.0f}s elapsed", file=sys.stderr)

            if batch:
                with conn.begin():
                    conn.execute(INSERT_SQL, batch)
                rows_inserted += len(batch)
        finally:
            conn.close()

    elapsed = time.monotonic() - start
    print(f"Done in {elapsed:.0f}s. {rows_seen:,} rows scanned, {rows_inserted:,} inserted into fno_bhavcopy.", file=sys.stderr)
    return rows_inserted


def verify(engine=None) -> None:
    """Cheap sanity check after import: row count + a known-good spot check."""
    engine = engine or _default_engine
    with engine.connect() as conn:
        total = conn.execute(text("SELECT COUNT(*) FROM fno_bhavcopy")).scalar()
        symbols = conn.execute(text("SELECT COUNT(DISTINCT symbol) FROM fno_bhavcopy")).scalar()
        sample = conn.execute(
            text(
                "SELECT symbol, instrument, trade_date, close FROM fno_bhavcopy "
                "WHERE symbol='NIFTY' AND instrument='FUTIDX' ORDER BY trade_date LIMIT 1"
            )
        ).fetchone()
    print(f"Verify: {total:,} total rows | {symbols:,} distinct symbols | "
          f"earliest NIFTY FUTIDX row: {sample}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", required=True, help="Path to the raw bhavcopy CSV")
    parser.add_argument("--batch-size", type=int, default=50_000)
    parser.add_argument("--truncate", action="store_true",
                         help="TRUNCATE fno_bhavcopy before importing (fresh load). Default: append.")
    args = parser.parse_args()

    print(f"Streaming {args.source} into MySQL fno_bhavcopy (single pass, bounded memory)...", file=sys.stderr)
    import_csv(args.source, batch_size=args.batch_size, truncate_first=args.truncate)
    verify()


if __name__ == "__main__":
    main()
