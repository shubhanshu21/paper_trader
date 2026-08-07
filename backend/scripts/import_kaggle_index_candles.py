"""
scripts/import_kaggle_index_candles.py — Import real 1-minute NIFTY/
BANKNIFTY underlying OHLC candles into the `index_1min_candles` MySQL
table (see db/models.py::Index1MinCandle) from several free Kaggle-
hosted datasets, downloaded into kaggle/ at the repo root.

Underlying-only, NOT options — this fills the gap fno_bhavcopy can't
(daily-only granularity means it can never validate an intraday signal
like Zero to Hero's PDH/PDL breakout or a Supertrend flip). It does NOT
solve real option-premium backtesting; that's a separate, harder,
still-unsolved sourcing problem (real vendor data or a Black-76 synthetic
reconstruction — see the conversation this script came out of).

SOURCE FILES AND WHY EACH ONE WAS CHOSEN FOR ITS DATE RANGE
-------------------------------------------------------------------------
Several of the 5 downloaded datasets overlap the same symbol/period —
inserting all of them raw would create duplicate/conflicting OHLC for
the same minute. Instead each (symbol, date range) has exactly ONE
authoritative source, picked for cleanliness/depth after inspecting all
five (see this repo's conversation history for the comparison):

  NIFTY:
    - kaggle/nifty-one-min-data-for-10-years/nifty_tick_ten.csv
      2015-01-09 .. 2025-01-31 (single clean file, spot-checked continuous)
    - kaggle/nifty-50-high-frequency-1-min-historical-data/Nifty50_2Years_1Min.csv
      2025-02-01 .. present (picks up exactly where the 10-year file ends)

  BANKNIFTY:
    - kaggle/nifty-intraday-1-min/Intraday 1 Min Data/**/*BNF*.txt (+ "Upto 2011")
      .. 2023-01-31 (deepest BankNifty history available across all 5 sources)
    - kaggle/bank-nifty-1-min-ohlcv-2-year-time-series-dataset/BankNifty_2Years_1Min.csv
      2024-04-23 .. present

  KNOWN GAP — NOT FILLED: BankNifty has NO source for 2023-02-01 ..
  2024-04-22 (~14 months). None of the 5 downloaded datasets cover it.
  Any backtest spanning that window will silently have zero BankNifty
  candles there — callers must check for that, this script does not
  fabricate a bridge.

  DELIBERATELY EXCLUDED: kaggle/nifty-bank-minute-data/ (the 8
  indicator-bloated CSVs, BankNifty 2015-2022) — same symbol/period the
  intraday-1-min archive already covers better, PLUS a confirmed data
  defect (492 rows with impossible post-15:30 IST timestamps, up to
  19:14) found by inspection. Strictly worse than what's already used
  for this symbol/period, not a second opinion worth keeping.

Idempotent: the (symbol, ts) unique index means a re-run safely no-ops
on rows already present (INSERT IGNORE) rather than erroring or
duplicating — same operational safety as import_bhavcopy_to_db.py.

Usage:
    python3 scripts/import_kaggle_index_candles.py
    python3 scripts/import_kaggle_index_candles.py --verify-only
"""
import argparse
import csv
import glob
import sys
import time
from datetime import datetime

from sqlalchemy import text

from db.engine import engine as _default_engine

KAGGLE_DIR = "../kaggle"  # repo root, not backend/ — this script runs with backend/ as cwd, same as import_bhavcopy_to_db.py

INSERT_SQL = text(
    """
    INSERT IGNORE INTO index_1min_candles
    (symbol, ts, open, high, low, close, volume, oi, source)
    VALUES
    (:symbol, :ts, :open, :high, :low, :close, :volume, :oi, :source)
    """
)


def _to_float(raw) -> float | None:
    try:
        return float(raw)
    except (ValueError, TypeError):
        return None


def _to_int(raw) -> int | None:
    try:
        return int(float(raw))
    except (ValueError, TypeError):
        return None


def _insert_batch(conn, batch: list[dict]) -> None:
    if not batch:
        return
    with conn.begin():
        conn.execute(INSERT_SQL, batch)


def _rows_from_iso_csv(path: str, symbol: str, source: str, min_ts: str | None = None, max_ts: str | None = None):
    """
    Rows from the two `time,open,high,low,close,volume` / `datetime,...,volume,oi`
    ISO-8601-timestamped CSVs (Nifty50_2Years_1Min.csv, BankNifty_2Years_1Min.csv,
    nifty_tick_ten.csv) — all three share this shape closely enough for one parser.
    `min_ts`/`max_ts` (both 'YYYY-MM-DD') enforce this source's assigned date range
    even if the file itself runs slightly outside it.
    """
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        ts_field = "time" if "time" in (reader.fieldnames or []) else "datetime"
        for row in reader:
            raw_ts = row.get(ts_field)
            if not raw_ts:
                continue
            try:
                ts = datetime.fromisoformat(raw_ts)
            except ValueError:
                continue
            ts_naive = ts.replace(tzinfo=None)
            date_str = ts_naive.date().isoformat()
            if min_ts and date_str < min_ts:
                continue
            if max_ts and date_str > max_ts:
                continue
            o, h, lo, c = _to_float(row.get("open")), _to_float(row.get("high")), _to_float(row.get("low")), _to_float(row.get("close"))
            if None in (o, h, lo, c):
                continue
            yield {
                "symbol": symbol, "ts": ts_naive, "open": o, "high": h, "low": lo, "close": c,
                "volume": _to_int(row.get("volume")), "oi": _to_int(row.get("oi")), "source": source,
            }


def _rows_from_intraday_txt(path: str, symbol: str, source: str):
    """
    Rows from nifty-intraday-1-min's `SYMBOL,YYYYMMDD,HH:MM,O,H,L,C,VOL,OI`
    headerless txt files (one per symbol per month, e.g. "2020 JAN NIFTY.txt").
    """
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        for line in csv.reader(f):
            if len(line) < 7:
                continue
            _sym, date_raw, time_raw, o_raw, h_raw, l_raw, c_raw = line[:7]
            vol_raw = line[7] if len(line) > 7 else None
            oi_raw = line[8] if len(line) > 8 else None
            try:
                ts = datetime.strptime(f"{date_raw} {time_raw}", "%Y%m%d %H:%M")
            except ValueError:
                continue
            o, h, lo, c = _to_float(o_raw), _to_float(h_raw), _to_float(l_raw), _to_float(c_raw)
            if None in (o, h, lo, c):
                continue
            yield {
                "symbol": symbol, "ts": ts, "open": o, "high": h, "low": lo, "close": c,
                "volume": _to_int(vol_raw), "oi": _to_int(oi_raw), "source": source,
            }


def _ingest(conn, rows_iter, batch_size: int = 20_000) -> tuple[int, int]:
    seen = inserted = 0
    batch = []
    for row in rows_iter:
        seen += 1
        batch.append(row)
        if len(batch) >= batch_size:
            _insert_batch(conn, batch)
            inserted += len(batch)
            batch.clear()
    _insert_batch(conn, batch)
    inserted += len(batch)
    return seen, inserted


def import_all(engine=None) -> None:
    engine = engine or _default_engine
    start = time.monotonic()
    conn = engine.connect()
    try:
        # --- NIFTY: 10-year file (2015-01-09 .. 2025-01-31) ---
        path = f"{KAGGLE_DIR}/nifty-one-min-data-for-10-years/nifty_tick_ten.csv"
        print(f"Importing NIFTY from {path} ...", file=sys.stderr)
        seen, inserted = _ingest(conn, _rows_from_iso_csv(path, "NIFTY", "nifty-one-min-data-for-10-years", max_ts="2025-01-31"))
        print(f"  {seen:,} scanned, {inserted:,} inserted.", file=sys.stderr)

        # --- NIFTY: recent file, only the days after the 10-year file ends ---
        path = f"{KAGGLE_DIR}/nifty-50-high-frequency-1-min-historical-data/Nifty50_2Years_1Min.csv"
        print(f"Importing NIFTY from {path} ...", file=sys.stderr)
        seen, inserted = _ingest(conn, _rows_from_iso_csv(path, "NIFTY", "nifty-50-high-frequency-1-min-historical-data", min_ts="2025-02-01"))
        print(f"  {seen:,} scanned, {inserted:,} inserted.", file=sys.stderr)

        # --- BANKNIFTY: intraday archive (.. 2023-01-31), every BankNifty file ---
        # Two real gotchas in this dataset's file naming/layout, found by
        # inspection, not assumption:
        #   - Most files use "BNF" in the name, but the pre-2011 file is
        #     "UPTO 2011 BANKNIFTY.txt" — filter on EITHER token.
        #   - "Consolidated/" duplicates files already present in the
        #     per-year folders — excluded, or every row gets scanned
        #     twice (INSERT IGNORE makes it harmless but wasteful).
        all_txt = glob.glob(f"{KAGGLE_DIR}/nifty-intraday-1-min/Intraday 1 Min Data/**/*.txt", recursive=True)
        bnf_files = sorted(
            p for p in all_txt
            if "/Consolidated/" not in p and ("BNF" in p.upper() or "BANKNIFTY" in p.upper())
        )
        print(f"Importing BANKNIFTY from {len(bnf_files)} intraday-archive files ...", file=sys.stderr)
        total_seen = total_inserted = 0
        for fpath in bnf_files:
            seen, inserted = _ingest(conn, _rows_from_intraday_txt(fpath, "BANKNIFTY", "nifty-intraday-1-min"))
            total_seen += seen
            total_inserted += inserted
        print(f"  {total_seen:,} scanned, {total_inserted:,} inserted.", file=sys.stderr)

        # --- BANKNIFTY: recent file (2024-04-23 .. present) ---
        path = f"{KAGGLE_DIR}/bank-nifty-1-min-ohlcv-2-year-time-series-dataset/BankNifty_2Years_1Min.csv"
        print(f"Importing BANKNIFTY from {path} ...", file=sys.stderr)
        seen, inserted = _ingest(conn, _rows_from_iso_csv(path, "BANKNIFTY", "bank-nifty-1-min-ohlcv-2-year-time-series-dataset"))
        print(f"  {seen:,} scanned, {inserted:,} inserted.", file=sys.stderr)
    finally:
        conn.close()

    elapsed = time.monotonic() - start
    print(f"Done in {elapsed:.0f}s.", file=sys.stderr)


def verify(engine=None) -> None:
    engine = engine or _default_engine
    with engine.connect() as conn:
        for symbol in ("NIFTY", "BANKNIFTY"):
            total = conn.execute(text("SELECT COUNT(*) FROM index_1min_candles WHERE symbol = :s"), {"s": symbol}).scalar()
            bounds = conn.execute(text("SELECT MIN(ts), MAX(ts) FROM index_1min_candles WHERE symbol = :s"), {"s": symbol}).first()
            print(f"{symbol}: {total:,} rows, {bounds[0]} .. {bounds[1]}")

        gap = conn.execute(text(
            "SELECT COUNT(*) FROM index_1min_candles WHERE symbol = 'BANKNIFTY' AND ts >= '2023-02-01' AND ts < '2024-04-23'"
        )).scalar()
        print(f"BANKNIFTY rows in the known 2023-02-01..2024-04-23 gap: {gap:,} (expected 0)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-only", action="store_true", help="Skip import, just report current table stats.")
    args = parser.parse_args()

    if not args.verify_only:
        import_all()
    verify()
