"""
scripts/fill_bhavcopy_gap.py — Download official F&O bhavcopies and load directly into MySQL fno_bhavcopy table.
"""
import argparse
import contextlib
import csv
import io
import sys
import time
import zipfile
from datetime import date, datetime, timedelta

import requests
from sqlalchemy import text

from automate.db.engine import get_session

NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}

# NEW format instrument-type code -> our fno_bhavcopy.instrument value.
_FININSTRMTP_MAP = {"STF": "FUTSTK", "STO": "OPTSTK", "IDF": "FUTIDX", "IDO": "OPTIDX"}

_TIMEOUT = 20
_RETRY_DELAY = 2.0
_MAX_RETRIES = 3


def _old_url(d: date) -> str:
    mon = d.strftime("%b").upper()
    return (
        f"https://nsearchives.nseindia.com/content/historical/DERIVATIVES/"
        f"{d.year}/{mon}/fo{d.day:02d}{mon}{d.year}bhav.csv.zip"
    )


def _new_url(d: date) -> str:
    return (
        f"https://nsearchives.nseindia.com/content/fo/"
        f"BhavCopy_NSE_FO_0_0_0_{d.strftime('%Y%m%d')}_F_0000.csv.zip"
    )


def _fetch_zip_csv(session: requests.Session, url: str) -> bytes | None:
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            resp = session.get(url, timeout=_TIMEOUT)
        except requests.exceptions.RequestException:
            if attempt < _MAX_RETRIES:
                time.sleep(_RETRY_DELAY)
                continue
            return None
        if resp.status_code == 404:
            return None  # No bhavcopy that day (weekend/holiday) or wrong URL pattern.
        if resp.status_code != 200:
            if attempt < _MAX_RETRIES:
                time.sleep(_RETRY_DELAY)
                continue
            return None
        try:
            z = zipfile.ZipFile(io.BytesIO(resp.content))
            name = z.namelist()[0]
            return z.read(name)
        except (zipfile.BadZipFile, IndexError):
            return None
    return None


def _rows_from_old_format(raw: bytes, trade_date: date):
    text_data = raw.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text_data))
    for row in reader:
        yield {
            "instrument": row["INSTRUMENT"],
            "symbol": row["SYMBOL"],
            "expiry_dt": _parse_old_date(row["EXPIRY_DT"]),
            "strike_pr": _to_float(row["STRIKE_PR"]),
            "option_typ": row["OPTION_TYP"],
            "open": _to_float(row["OPEN"]),
            "high": _to_float(row["HIGH"]),
            "low": _to_float(row["LOW"]),
            "close": _to_float(row["CLOSE"]),
            "settle_pr": _to_float(row["SETTLE_PR"]),
            "contracts": _to_int(row["CONTRACTS"]),
            "val_inlakh": _to_float(row["VAL_INLAKH"]),
            "open_int": _to_int(row["OPEN_INT"]),
            "chg_in_oi": _to_int(row["CHG_IN_OI"]),
            "trade_date": trade_date.isoformat(),
        }


def _rows_from_new_format(raw: bytes, trade_date: date):
    text_data = raw.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text_data))
    for row in reader:
        instrument = _FININSTRMTP_MAP.get(row.get("FinInstrmTp", ""))
        if instrument is None:
            continue
        val_inlakh = _to_float(row.get("TtlTrfVal"))
        yield {
            "instrument": instrument,
            "symbol": row["TckrSymb"],
            "expiry_dt": row.get("XpryDt") or None,
            "strike_pr": _to_float(row["StrkPric"]),
            "option_typ": row.get("OptnTp") or "XX",
            "open": _to_float(row["OpnPric"]),
            "high": _to_float(row["HghPric"]),
            "low": _to_float(row["LwPric"]),
            "close": _to_float(row["ClsPric"]),
            "settle_pr": _to_float(row["SttlmPric"]),
            "contracts": _to_int(row["TtlTradgVol"]),
            "val_inlakh": (val_inlakh / 100000.0) if val_inlakh is not None else None,
            "open_int": _to_int(row["OpnIntrst"]),
            "chg_in_oi": _to_int(row["ChngInOpnIntrst"]),
            "trade_date": trade_date.isoformat(),
        }


def _parse_old_date(raw: str) -> str | None:
    for fmt in ["%d-%b-%Y", "%d-%b-%y"]:
        try:
            return datetime.strptime(raw.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


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


def already_have(session, trade_date: date) -> bool:
    row = session.execute(
        text("SELECT 1 FROM fno_bhavcopy WHERE trade_date = :trade_date LIMIT 1"),
        {"trade_date": trade_date.isoformat()},
    ).fetchone()
    return row is not None


def import_day(session, http_session: requests.Session, d: date) -> str:
    """Returns 'imported' | 'no_data' | 'skipped_existing'."""
    if already_have(session, d):
        return "skipped_existing"

    raw = _fetch_zip_csv(http_session, _new_url(d))
    rows_fn = _rows_from_new_format
    if raw is None:
        raw = _fetch_zip_csv(http_session, _old_url(d))
        rows_fn = _rows_from_old_format
    if raw is None:
        return "no_data"

    insert_sql = text("""
        INSERT INTO fno_bhavcopy
        (instrument, symbol, expiry_dt, strike_pr, option_typ, open, high, low,
         close, settle_pr, contracts, val_inlakh, open_int, chg_in_oi, trade_date)
        VALUES (:instrument, :symbol, :expiry_dt, :strike_pr, :option_typ, :open, :high, :low,
         :close, :settle_pr, :contracts, :val_inlakh, :open_int, :chg_in_oi, :trade_date)
    """)
    batch = list(rows_fn(raw, d))
    if batch:
        session.execute(insert_sql, batch)
    return "imported"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--to-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--delay", type=float, default=0.4, help="Seconds between requests")
    args = parser.parse_args()

    http_session = requests.Session()
    http_session.headers.update(NSE_HEADERS)
    with contextlib.suppress(requests.exceptions.RequestException):
        http_session.get("https://www.nseindia.com", timeout=15)  # Warm-up for cookies.

    start = datetime.strptime(args.from_date, "%Y-%m-%d").date()
    end = datetime.strptime(args.to_date, "%Y-%m-%d").date()

    imported_days = 0
    no_data_days = 0
    skipped_days = 0
    d = start
    t0 = time.monotonic()

    with get_session() as session:
        while d <= end:
            if d.weekday() < 5:  # Skip Sat/Sun (bhavcopy never exists)
                status = import_day(session, http_session, d)
                if status == "imported":
                    imported_days += 1
                elif status == "no_data":
                    no_data_days += 1
                else:
                    skipped_days += 1
                time.sleep(args.delay)
            d += timedelta(days=1)

            if d.day == 1:  # Progress once a month.
                elapsed = time.monotonic() - t0
                print(f"  ... up to {d.isoformat()} | imported={imported_days} no_data={no_data_days} "
                      f"skipped(existing)={skipped_days} | {elapsed:.0f}s elapsed", file=sys.stderr)

    print(f"Done. imported={imported_days} no_data(weekend/holiday/unavailable)={no_data_days} "
          f"skipped(already had)={skipped_days}", file=sys.stderr)


if __name__ == "__main__":
    main()
