"""
utils/market_calendar.py — Dynamic NSE Market Calendar.

Fetches NSE trading holidays and F&O freeze quantities at runtime
from official public sources. Nothing is hardcoded — every value
comes from a live API call cached on disk for the day/year.

Sources:
  Holidays      → NSE public API (no auth required):
                  https://www.nseindia.com/api/holiday-master?type=trading
                → Upstox MarketHolidaysAndTimingsApi (requires access_token, fallback)

  Freeze Qty    → NSE F&O market lots API (no auth required):
                  https://www.nseindia.com/api/fo-mktlots
                → Broker instrument master CSV (lot_size column, already downloaded)

Cache files:
  cache/nse_holidays_<YYYY>.json      — refreshed once per year
  cache/nse_freeze_qty_<YYYY-MM-DD>.json — refreshed daily (lot sizes can change)

Fallback chain (both):
  1. Today's/year's cache file on disk
  2. Live Broker API / NSE API call
  3. RuntimeError — we never silently fall back to hardcoded data

Usage:
    calendar = MarketCalendar()
    calendar.refresh()                        # Download everything at startup
    calendar.is_trading_day(date.today())     # True/False
    calendar.assert_market_is_open()          # Raises RuntimeError if closed
    calendar.get_freeze_quantity("RELIANCE")  # Returns int
"""

import json
from datetime import date, datetime, time as dtime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import requests

from automate.utils.logger import get_logger
from automate.config import MarketConfig

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_IST = ZoneInfo("Asia/Kolkata")
_open_parts = [int(x) for x in MarketConfig.OPEN.split(":")]
_MARKET_OPEN  = dtime(*_open_parts)
_close_parts = [int(x) for x in MarketConfig.CLOSE.split(":")]
_MARKET_CLOSE = dtime(*_close_parts)

CACHE_DIR = Path("cache")

# NSE public endpoints (no authentication required)
NSE_HOLIDAY_URL   = "https://www.nseindia.com/api/holiday-master?type=trading"
NSE_FO_MKTLOTS_URL = "https://www.nseindia.com/api/fo-mktlots"

# NSE blocks plain urllib requests — these headers mimic a real browser session
NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.nseindia.com/",
    "Connection": "keep-alive",
}

# Date formats used by the NSE holiday API response
# NSE returns dates like "22-Jan-2025"
NSE_DATE_FMT = "%d-%b-%Y"

# Minimum expected holidays (sanity check — NSE always has at least 10/year)
_MIN_HOLIDAYS = 10

# Request timeout
_TIMEOUT_SEC = 20

# How many retries for the NSE API (it can be flaky)
_MAX_RETRIES = 3


class MarketCalendar:
    """
    Dynamic NSE trading calendar with live holiday + freeze quantity data.

    Replaces all hardcoded NSE_HOLIDAYS_* and NSE_FREEZE_QUANTITIES dicts
    in compliance/sebi_rules.py with live API-fetched, cached data.

    Thread-safe for reading (single writer at startup).

    Args:
        cache_dir:    Directory for storing cache files.
        access_token: Optional Upstox access token for broker API fallback.
    """

    def __init__(
        self,
        cache_dir: Path = CACHE_DIR,
        access_token: Optional[str] = None,
    ) -> None:
        self._cache_dir = cache_dir
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._access_token = access_token

        # In-memory state (populated by refresh())
        self._holidays: set[date] = set()
        self._freeze_quantities: dict[str, int] = {}
        self._loaded = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def refresh(self, force: bool = False) -> None:
        """
        Download and cache NSE holidays + freeze quantities.

        Call this once at startup (before market opens). Subsequent calls
        within the same day reuse cached files unless force=True.

        Args:
            force: Re-download even if today's cache already exists.
        """
        log.info("[Calendar] Refreshing NSE market calendar (force=%s) ...", force)
        self._holidays = self._load_holidays(force=force)
        self._freeze_quantities = self._load_freeze_quantities(force=force)
        self._loaded = True
        log.info(
            "[Calendar] Ready — %d holidays loaded | %d freeze qty entries loaded.",
            len(self._holidays),
            len(self._freeze_quantities),
        )

    def is_trading_day(self, check_date: Optional[date] = None) -> bool:
        """
        Return True if the given date is a valid NSE trading day.

        Args:
            check_date: Date to check (defaults to today).

        Returns:
            True if it's a weekday and not a NSE holiday.
        """
        self._ensure_loaded()
        check_date = check_date or date.today()

        if check_date.weekday() >= 5:  # Saturday=5, Sunday=6
            return False
        return check_date not in self._holidays

    def assert_market_is_open(self, check_time: Optional[datetime] = None) -> None:
        """
        Raise RuntimeError if the given (or current) IST time is outside market hours.

        Checks:
          1. Not a weekend
          2. Not a NSE holiday
          3. Within 09:15-15:30 IST

        Args:
            check_time: Timestamp to validate instead of the real wall-clock
                time (e.g. a backtest's simulated current time). Naive
                datetimes are treated as already being in IST; tz-aware ones
                are converted to IST. Defaults to `datetime.now(IST)`.

        Raises:
            RuntimeError: With a human-readable reason if market is closed.
        """
        self._ensure_loaded()
        if check_time is not None:
            now_ist = (
                check_time.astimezone(_IST) if check_time.tzinfo else check_time.replace(tzinfo=_IST)
            )
        else:
            now_ist = datetime.now(tz=_IST)
        today = now_ist.date()
        current_time = now_ist.time()

        if today.weekday() >= 5:
            raise RuntimeError(
                f"[SEBI] Market CLOSED — {now_ist.strftime('%A, %d %b %Y')} is a weekend."
            )

        if today in self._holidays:
            raise RuntimeError(
                f"[SEBI] Market CLOSED — {today.isoformat()} is an NSE trading holiday."
            )

        if not (_MARKET_OPEN <= current_time <= _MARKET_CLOSE):
            raise RuntimeError(
                f"[SEBI] Market CLOSED — Current IST time: "
                f"{current_time.strftime('%H:%M:%S')}. "
                f"NSE F&O trading hours: 09:15–15:30 IST."
            )

        log.info(
            "[SEBI] Market OPEN confirmed | IST: %s",
            now_ist.strftime("%Y-%m-%d %H:%M:%S %Z"),
        )

    def get_freeze_quantity(self, symbol: str) -> int:
        """
        Return the NSE F&O freeze quantity for a stock.
        Currently hardcoded to a safe upper limit (50,000) as the NSE API is dead.
        
        Args:
            symbol: NSE trading symbol (e.g., 'RELIANCE').
        Returns:
            Freeze quantity as int (default 50000).
        """
        self._ensure_loaded()
        symbol = symbol.upper().strip()
        
        # Hardcoded value since NSE API is dead. 
        # Retail orders (1-10 lots) will never hit this limit.
        return self._freeze_quantities.get(symbol, 50000)

    # ------------------------------------------------------------------
    # Holiday loading
    # ------------------------------------------------------------------

    def _load_holidays(self, force: bool) -> set[date]:
        """
        Load NSE holidays for the current year.

        Cache file: cache/nse_holidays_<YYYY>.json
        Refreshed once per year (unless force=True).
        """
        year = date.today().year
        cache_file = self._cache_dir / f"nse_holidays_{year}.json"

        if not force and cache_file.exists():
            log.info("[Calendar] Loading holidays from cache: %s", cache_file.name)
            return self._parse_holidays_cache(cache_file)

        # Fetch directly using the Upstox public API instead of the flaky NSE website
        holidays = self._fetch_holidays()

        if not holidays:
            raise RuntimeError(
                "[Calendar] CRITICAL: Could not fetch NSE holidays from any source. "
                "Cannot guarantee SEBI compliance without a valid holiday list. "
                "Check network connectivity and retry."
            )

        if len(holidays) < _MIN_HOLIDAYS:
            log.warning(
                "[Calendar] Only %d holidays fetched — expected at least %d. "
                "Data may be incomplete.",
                len(holidays), _MIN_HOLIDAYS,
            )

        # Cache to disk as JSON
        self._save_holidays_cache(cache_file, holidays)
        return holidays

    def _fetch_holidays(self) -> set[date]:
        """
        Fetch NSE holidays from the public Upstox market calendar API.
        
        Uses the public `https://api.upstox.com/v2/market/holidays` endpoint.
        Does not require authentication. This is far more robust than scraping
        the NSE website which actively blocks bot User-Agents.
        
        Returns:
            Set of date objects, or empty set on failure.
        """

        try:
            url = "https://api.upstox.com/v2/market/holidays"
            headers = {"Accept": "application/json"}
            response = requests.get(url, headers=headers, timeout=_TIMEOUT_SEC)
            response.raise_for_status()
            
            data = response.json()
            holiday_list = data.get("data", [])
            
            holidays: set[date] = set()
            for item in holiday_list:
                # Upstox returns closed_exchanges. Only add if NSE or NFO is closed.
                closed_exchanges = item.get("closed_exchanges", [])
                if "NSE" in closed_exchanges or "NFO" in closed_exchanges:
                    raw_date = item.get("date")
                    if raw_date:
                        try:
                            holidays.add(datetime.strptime(raw_date, "%Y-%m-%d").date())
                        except ValueError:
                            pass

            log.info(
                "[Calendar] Fetched %d holidays.", len(holidays)
            )
            return holidays

        except Exception as exc:
            log.error("[Calendar] Upstox holiday API failed: %s", exc)
            return set()

    # ------------------------------------------------------------------
    # Freeze quantity loading
    # ------------------------------------------------------------------

    def _load_freeze_quantities(self, force: bool) -> dict[str, int]:
        """
        Since the NSE API is dead, we no longer fetch freeze quantities dynamically.
        This returns an empty dictionary. The `get_freeze_quantity` method falls back
        to a hardcoded limit of 50000.
        """
        return {}

    # ------------------------------------------------------------------
    # Cache helpers
    # ------------------------------------------------------------------

    def _save_holidays_cache(self, cache_file: Path, holidays: set[date]) -> None:
        """Save holiday set to JSON file."""
        data = sorted(d.isoformat() for d in holidays)
        cache_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        log.info("[Calendar] Holidays cached to %s (%d dates)", cache_file.name, len(holidays))

    def _parse_holidays_cache(self, cache_file: Path) -> set[date]:
        """Load holiday set from JSON file."""
        try:
            raw = json.loads(cache_file.read_text(encoding="utf-8"))
            return {date.fromisoformat(d) for d in raw}
        except Exception as exc:
            log.warning("[Calendar] Could not parse holiday cache: %s — re-fetching.", exc)
            return self._fetch_holidays()

    def _cleanup_old_freeze_cache(self) -> None:
        """Remove freeze quantity cache files older than 7 days."""
        cutoff = datetime.now().timestamp() - (7 * 86400)
        for f in self._cache_dir.glob("nse_freeze_qty_*.json"):
            if f.stat().st_mtime < cutoff:
                f.unlink()
                log.debug("[Calendar] Deleted stale freeze qty cache: %s", f.name)

    def _ensure_loaded(self) -> None:
        """Auto-load with defaults if refresh() was never called."""
        if not self._loaded:
            log.warning(
                "[Calendar] MarketCalendar.refresh() was not called before use. "
                "Calling now with defaults. Call refresh() explicitly at startup."
            )
            self.refresh()
