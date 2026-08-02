"""
scripts/import_historical_csvs_to_db.py — Import historical CSVs into MySQL candles table.
"""
import argparse
import csv
import re
import sys
from pathlib import Path
from typing import Optional

from sqlalchemy import text

from automate.db.engine import get_session

_FILENAME_RE = re.compile(r"^(?P<symbol>[A-Z0-9]+)_(?P<leg>spot|CE|PE)_.+\.csv$")
_BHAVCOPY_RE = re.compile(r"^(?P<symbol>[A-Z0-9]+)_daily_bhavcopy\.csv$")


def parse_filename(name: str):
    m = _FILENAME_RE.match(name)
    if m:
        return m.group("symbol"), m.group("leg")
    m = _BHAVCOPY_RE.match(name)
    if m:
        return m.group("symbol"), "spot"
    return None, None


def import_dir(source_dir: str) -> int:
    insert_sql = text("""
        INSERT INTO candles (symbol, leg, source_file, timestamp, open, high, low, close, volume, open_interest)
        VALUES (:symbol, :leg, :source_file, :timestamp, :open, :high, :low, :close, :volume, :open_interest)
    """)

    delete_sql = text("""
        DELETE FROM candles WHERE source_file = :source_file
    """)

    total_rows = 0
    files_imported = 0

    with get_session() as session:
        for csv_path in sorted(Path(source_dir).glob("*.csv")):
            symbol, leg = parse_filename(csv_path.name)
            if symbol is None:
                print(f"Skipping {csv_path.name} — doesn't match a known filename pattern.", file=sys.stderr)
                continue

            # Idempotent re-runs: replace this file's rows
            session.execute(delete_sql, {"source_file": csv_path.name})

            with open(csv_path, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                batch = []
                for row in reader:
                    batch.append({
                        "symbol": symbol,
                        "leg": leg,
                        "source_file": csv_path.name,
                        "timestamp": row["timestamp"],
                        "open": float(row["open"]),
                        "high": float(row["high"]),
                        "low": float(row["low"]),
                        "close": float(row["close"]),
                        "volume": float(row["volume"]) if row.get("volume") not in ("", None) else None,
                        "open_interest": float(row["open_interest"]) if row.get("open_interest") not in ("", None) else None,
                    })

            if batch:
                session.execute(insert_sql, batch)
                total_rows += len(batch)
                files_imported += 1
                print(f"  {csv_path.name}: symbol={symbol} leg={leg} rows={len(batch)}", file=sys.stderr)

    print(f"Imported {files_imported} files, {total_rows:,} rows into MySQL (table: candles).", file=sys.stderr)
    return total_rows


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dir", default="data/historical", help="Directory of CSVs to import")
    args = parser.parse_args()
    import_dir(args.dir)


if __name__ == "__main__":
    main()
