"""
compliance/sebi_rules.py — SEBI Algorithmic Trading Compliance Engine.

This module implements mandatory regulatory controls as required by SEBI
for algorithmic / automated trading by retail clients in India.

Key SEBI circulars implemented:
  ─ SEBI/MRD/DP/POLICY/P/2013/26 (Algo Trading Framework)
  ─ SEBI/HO/MRD/DP/CIR/P/2016/137 (Kill Switch mandate)
  ─ SEBI/HO/MRD1/MTMR/CIR/P/2021/536 (Retail Algo / API trading)
  ─ NSE/BSE circular on F&O position limits for clients

Controls implemented:
  1. Market Hours Gate        — only execute within NSE F&O session
  2. Order Rate Limiter       — max 10 orders/sec per SEBI retail API cap
  3. Pre-Trade Price Band     — reject orders outside ±20% circuit limit
  4. Max Order Quantity       — enforce lot-size / freeze-quantity cap
  5. Kill Switch              — atomic flag to halt all order flow
  6. Audit Trail Logger       — immutable record of every order attempt
  7. Position Limit Warning   — flag if near SEBI single-stock F&O limits
  8. Disclaimer               — mandatory risk disclosure on startup

Dynamic data (no hardcoded values):
  - NSE trading holidays    → fetched from NSE public API, cached annually
                              (utils/market_calendar.py)
  - F&O freeze quantities   → fetched from NSE F&O market lots API, cached daily
                              (utils/market_calendar.py)

IMPORTANT — READ BEFORE USING:
  ─ Retail algorithmic trading via broker APIs requires your algorithm to
    be registered / whitelisted by your broker. Ensure you have completed
    the algo registration process before going live.
  ─ Selling (writing) options requires sufficient SPAN + Exposure margins.
    Always verify your available margin with your broker before execution.
  ─ This script does NOT guarantee profits. Options selling carries
    unlimited theoretical risk on the Call leg and high risk on the Put
    leg. Use at your own risk.
"""

import threading
import time
from collections import deque
from datetime import datetime
from datetime import time as dtime
from zoneinfo import ZoneInfo

from config import MarketConfig
from utils.logger import get_logger
from utils.market_calendar import MarketCalendar

log = get_logger(__name__)

class ComplianceError(Exception):
    """Exception raised when a SEBI pre-trade compliance check fails."""


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# NSE F&O market hours in IST (Indian Standard Time = UTC+5:30)
_IST = ZoneInfo("Asia/Kolkata")
_open_parts = [int(x) for x in MarketConfig.OPEN.split(":")]
_MARKET_OPEN  = dtime(*_open_parts)
_close_parts = [int(x) for x in MarketConfig.CLOSE.split(":")]
_MARKET_CLOSE = dtime(*_close_parts)

# SEBI-mandated retail API order rate cap: 10 orders per second
_MAX_ORDERS_PER_SECOND = 10

# NSE single-stock F&O client-level position limit (SEBI):
# Lower of 1% of free-float market capitalisation OR ₹500 Cr notional value.
# For simplicity this module flags if lot count exceeds a conservative threshold.
_CLIENT_LOT_LIMIT_WARNING = 50  # Warn if > 50 lots (conservative; varies by stock)

# ---------------------------------------------------------------------------
# Module-level MarketCalendar singleton
# ---------------------------------------------------------------------------
# This singleton is shared by all compliance checks in this module.
# Call `init_market_calendar()` at startup (or it auto-refreshes on first use).
_market_calendar = MarketCalendar()


def init_market_calendar(
    force: bool = False,
    access_token: str = "",
) -> MarketCalendar:
    """
    Initialise (or re-initialise) the module-level market calendar.

    Call this once at application startup — before any compliance check.
    Downloads NSE holidays and F&O freeze quantities from live APIs.

    Args:
        force:        Re-download even if today's cache exists.
        access_token: Optional Upstox access_token for holiday API fallback.

    Returns:
        The initialised MarketCalendar singleton (for inspection/testing).
    """
    global _market_calendar
    _market_calendar = MarketCalendar(
        access_token=access_token if access_token else None,
    )
    _market_calendar.refresh(force=force)
    return _market_calendar


# ---------------------------------------------------------------------------
# 1. Kill Switch — SEBI/HO/MRD/DP/CIR/P/2016/137
# ---------------------------------------------------------------------------

class KillSwitch:
    """
    Thread-safe, atomic kill switch to halt all order flow immediately.

    SEBI mandates that all algorithmic trading systems have an accessible
    kill switch that can stop all order generation within one trading cycle.

    Usage:
        kill_switch = KillSwitch()
        kill_switch.activate()      # Halt all orders
        kill_switch.is_active()     # Check state (checked before every order)
        kill_switch.reset()         # Re-enable (manual re-enable only)
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active = False
        self._reason: str | None = None
        self._activated_at: datetime | None = None
        log.info("[SEBI] Kill switch initialised (state: OFF).")

    def activate(self, reason: str = "Manual trigger") -> None:
        """Activate the kill switch — all subsequent orders will be blocked."""
        with self._lock:
            if not self._active:
                self._active = True
                self._reason = reason
                self._activated_at = datetime.now()
                log.critical(
                    "[SEBI KILL SWITCH ACTIVATED] All order flow halted. Reason: %s",
                    reason,
                )

    def reset(self) -> None:
        """Deactivate the kill switch — requires explicit manual reset."""
        with self._lock:
            self._active = False
            self._reason = None
            self._activated_at = None
            log.warning("[SEBI] Kill switch RESET. Order flow re-enabled manually.")

    def is_active(self) -> bool:
        """Return True if the kill switch is currently engaged."""
        with self._lock:
            return self._active

    def status(self) -> dict:
        """Full state — the accessible kill switch's own status endpoint reads this, not just is_active(), so the UI can show WHY and WHEN it tripped."""
        with self._lock:
            return {
                "active": self._active,
                "reason": self._reason,
                "activated_at": self._activated_at.isoformat() if self._activated_at else None,
            }


# One shared instance for the ENTIRE process, not one per engine module.
# Every strategy engine used to build its own `KillSwitch()` in its own
# module — 11 separate instances, none of which shared state with any
# other, so activating "the" kill switch from any one place never
# actually touched the other 10. That made the whole SEBI-mandated
# "accessible kill switch that halts ALL order flow" requirement
# impossible to satisfy: there was no single switch to make accessible.
# Every engine constructor call site now passes this same object instead
# of instantiating its own.
_global_kill_switch = KillSwitch()


def get_global_kill_switch() -> KillSwitch:
    return _global_kill_switch


def assert_kill_switch_not_active(kill_switch: "KillSwitch | None") -> None:
    """
    Raise if `kill_switch` is currently active — the actual per-order
    enforcement point every engine's own order-placement method calls
    first, now that a kill switch is a real, shared, halts-everything
    control rather than an inert per-engine flag nothing checked.
    None is a pass-through (never guess whether one SHOULD have been
    wired in) — used by read-only preview engines (see
    routes_adjustment_preview.py) that never place a real order anyway.
    """
    if kill_switch is not None and kill_switch.is_active():
        raise RuntimeError(
            "[SEBI] KILL SWITCH IS ACTIVE — all order flow is halted. Reset it manually before retrying."
        )


# ---------------------------------------------------------------------------
# 2. Order Rate Limiter — max 10 orders/second (SEBI retail API cap)
# ---------------------------------------------------------------------------

class OrderRateLimiter:
    """
    Sliding-window rate limiter that enforces SEBI's 10 orders/second cap.

    Tracks order timestamps in a deque. Before each order, it checks whether
    the limit would be exceeded and blocks (sleeps) if necessary.
    """

    def __init__(self, max_per_second: int = _MAX_ORDERS_PER_SECOND) -> None:
        self._max = max_per_second
        self._timestamps: deque[float] = deque()
        self._lock = threading.Lock()
        log.info(
            "[SEBI] Order rate limiter initialised: max %d orders/second.",
            self._max,
        )

    def acquire(self) -> None:
        """
        Block until placing an order would not exceed the rate limit.
        Must be called immediately before every order placement.
        """
        with self._lock:
            now = time.monotonic()
            # Remove timestamps older than 1 second
            while self._timestamps and now - self._timestamps[0] > 1.0:
                self._timestamps.popleft()

            if len(self._timestamps) >= self._max:
                # Calculate wait time needed to slide the window
                sleep_for = 1.0 - (now - self._timestamps[0])
                if sleep_for > 0:
                    log.debug(
                        "[SEBI] Rate limit reached. Sleeping %.3fs before order.",
                        sleep_for,
                    )
                    time.sleep(sleep_for)

            self._timestamps.append(time.monotonic())


# Every strategy engine used to build ONE module-level OrderRateLimiter and
# share it across every user's strategies of that type — unlike the kill
# switch (deliberately global, see _global_kill_switch above), SEBI's 10
# orders/second cap is a PER-ACCOUNT limit, not a shared pool for the whole
# panel. Under the old shared instance, a burst of orders from one user's
# strategy could throttle/delay another user's real-money order placement
# or exit through the same bucket. get_rate_limiter_for(user_id) hands back
# a lazily-created, per-user limiter instead so each account's order flow
# only ever waits on its own recent order history.
_rate_limiters_lock = threading.Lock()
_rate_limiters_by_user: dict[int | None, "OrderRateLimiter"] = {}


def get_rate_limiter_for(user_id: int | None) -> "OrderRateLimiter":
    """
    Per-user OrderRateLimiter, created on first use and reused after that.
    user_id=None (no real per-request user — legacy CLI daemon, backtest)
    gets its own shared bucket, same as before this was scoped per user.
    """
    with _rate_limiters_lock:
        limiter = _rate_limiters_by_user.get(user_id)
        if limiter is None:
            limiter = OrderRateLimiter()
            _rate_limiters_by_user[user_id] = limiter
        return limiter


# ---------------------------------------------------------------------------
# 3. Market Hours Gate
# ---------------------------------------------------------------------------

def assert_market_is_open(check_time: datetime | None = None) -> None:
    """
    Raise a RuntimeError if the given (or current) time is outside NSE F&O trading hours.

    NSE F&O Market Hours (IST): 09:15 — 15:30, Monday–Friday.
    Holiday list is fetched live from the NSE public API (not hardcoded)
    and cached annually in cache/nse_holidays_<YYYY>.json.

    Args:
        check_time: Optional timestamp to validate instead of real time
            (used by backtests to check against simulated dates).

    Raises:
        RuntimeError: If current time is outside market hours or on a holiday.
    """
    # Delegate entirely to the live MarketCalendar (which checks weekend,
    # holiday list from NSE API, and market session time).
    _market_calendar.assert_market_is_open(check_time=check_time)


# ---------------------------------------------------------------------------
# 4. Pre-Trade Price Band Check (±20% circuit filter)
# ---------------------------------------------------------------------------

def validate_price_band(strike_price: int, spot_price: float) -> None:
    """
    Warn if a strike price is outside the ±20% circuit filter band.

    While the strategy intentionally places strikes at ±10% from spot,
    this guard verifies the computed strike does not breach exchange
    circuit limits (typically ±20% for stock F&O).

    Args:
        strike_price: The computed option strike (integer).
        spot_price:   Current LTP of the underlying.

    Raises:
        ValueError: If the strike is more than 20% away from spot.
    """
    deviation_pct = abs(strike_price - spot_price) / spot_price * 100

    if deviation_pct > 20.0:
        raise ValueError(
            f"[SEBI] Strike {strike_price} is {deviation_pct:.1f}% away from "
            f"spot ₹{spot_price:.2f}. This exceeds the ±20% F&O circuit limit. "
            f"Order rejected."
        )

    log.info(
        "[SEBI] Price band check PASSED — Strike %d is %.1f%% from spot ₹%.2f.",
        strike_price, deviation_pct, spot_price,
    )


# ---------------------------------------------------------------------------
# 5. Max Order Quantity / Freeze Quantity Check
# ---------------------------------------------------------------------------

def validate_order_quantity(symbol: str, quantity: int) -> None:
    """
    Ensure order quantity does not exceed NSE's per-order freeze quantity.

    NSE F&O has a 'freeze quantity' per stock — orders above this threshold
    must be auto-sliced (which the broker handles, but we log a warning).

    NSE's F&O market lots API is dead, so freeze quantities are no longer
    fetched dynamically: get_freeze_quantity() always returns a hardcoded
    safe upper bound (50,000) for symbols with no cached value. See
    utils/market_calendar.py's get_freeze_quantity() docstring.

    Args:
        symbol:   Stock symbol (e.g., 'RELIANCE').
        quantity: Total units being ordered.
    """
    freeze_qty = _market_calendar.get_freeze_quantity(symbol.upper())

    if quantity > freeze_qty:
        log.warning(
            "[SEBI] Order quantity %d exceeds NSE freeze quantity %d for %s. "
            "The broker's auto-slice feature (slice=True) will split this order.",
            quantity, freeze_qty, symbol,
        )
    else:
        log.info(
            "[SEBI] Order quantity check PASSED — %d ≤ freeze qty %d for %s.",
            quantity, freeze_qty, symbol,
        )


# ---------------------------------------------------------------------------
# 6. Position Limit Warning
# ---------------------------------------------------------------------------

def warn_position_limits(symbol: str, total_lots: int) -> None:
    """
    Emit a warning if the lot count approaches SEBI client-level position limits.

    SEBI caps client-level open interest in single-stock F&O at the lower of:
      - 1% of the number of shares held by non-promoters, OR
      - ₹500 crore notional value.

    The exact limit varies per stock. This function uses a conservative
    threshold (_CLIENT_LOT_LIMIT_WARNING) and warns accordingly.

    Args:
        symbol:     Stock symbol.
        total_lots: Total lots being traded (both legs combined).
    """
    if total_lots >= _CLIENT_LOT_LIMIT_WARNING:
        log.warning(
            "[SEBI] POSITION LIMIT WARNING — You are trading %d lots of %s. "
            "SEBI client-level position limits apply to single-stock F&O. "
            "Verify your current open interest with your broker before proceeding.",
            total_lots, symbol,
        )
    else:
        log.info(
            "[SEBI] Position limit check PASSED — %d lots of %s is within "
            "conservative threshold of %d.",
            total_lots, symbol, _CLIENT_LOT_LIMIT_WARNING,
        )


# ---------------------------------------------------------------------------
# 7. Audit Trail Logger
# ---------------------------------------------------------------------------

class AuditTrail:
    """
    Immutable, append-only audit logger for every order event.

    SEBI requires brokers and clients to maintain complete audit trails of
    all algorithmic orders. This writes each event to a dedicated audit log
    file, separate from the main operational log.

    Format:
        AUDIT | <timestamp> | <event_type> | <symbol> | <instrument_token> |
               <option_type> | <strike> | <quantity> | <order_id> | <status>
    """

    def __init__(self, audit_log_path: str = "logs/audit_trail.log") -> None:
        import logging as _logging
        from logging.handlers import TimedRotatingFileHandler
        from pathlib import Path

        Path(audit_log_path).parent.mkdir(parents=True, exist_ok=True)

        self._audit_logger = _logging.getLogger("AUDIT_TRAIL")
        self._audit_logger.setLevel(_logging.INFO)
        self._audit_logger.propagate = False  # Isolate from main logger

        if not self._audit_logger.handlers:
            handler = TimedRotatingFileHandler(
                audit_log_path,
                when="midnight",
                backupCount=365,  # Keep 1 year of audit records
                encoding="utf-8",
            )
            handler.setFormatter(
                _logging.Formatter("%(asctime)s | %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z")
            )
            self._audit_logger.addHandler(handler)

        log.info("[SEBI] Audit trail initialised → %s", audit_log_path)

    def record(
        self,
        event_type: str,
        symbol: str,
        instrument_token: str,
        option_type: str,
        strike: int,
        quantity: int,
        order_id: str = "N/A",
        status: str = "INITIATED",
        note: str = "",
    ) -> None:
        """Write one immutable audit record."""
        self._audit_logger.info(
            "AUDIT | %s | %s | %s | %s | strike=%d | qty=%d | order_id=%s | "
            "status=%s | %s",
            event_type,
            symbol,
            instrument_token,
            option_type,
            strike,
            quantity,
            order_id,
            status,
            note,
        )


# ---------------------------------------------------------------------------
# 8. Startup Risk Disclaimer (mandatory display)
# ---------------------------------------------------------------------------

def print_risk_disclaimer() -> None:
    """
    Print the mandatory SEBI risk disclosure for F&O trading.

    SEBI mandates that all F&O trading platforms display a risk disclosure.
    Reference: SEBI circular SEBI/HO/CDMRD/DOCO/CIR/P/2020/203
    """
    disclaimer = """
╔══════════════════════════════════════════════════════════════════════════════╗
║              SEBI MANDATORY RISK DISCLOSURE — F&O TRADING                  ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  1. 9 out of 10 individual traders in the equity F&O segment incur losses. ║
║     (Source: SEBI Study on Profit & Loss of Individual Traders, 2023)      ║
║  2. Trading in derivatives carries UNLIMITED RISK on short positions.       ║
║  3. You must have sufficient SPAN + Exposure margin before writing options. ║
║  4. Algo/automated trading via broker APIs must be registered with your     ║
║     broker as per SEBI circular SEBI/HO/MRD1/MTMR/CIR/P/2021/536.        ║
║  5. This software does NOT provide investment advice. Use at your own risk. ║
║  6. Past performance of a strategy does NOT guarantee future results.       ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  Regulatory Framework:                                                       ║
║  ─ SEBI (Stock Brokers) Regulations, 1992                                   ║
║  ─ SEBI Circular on Algo Trading (2013, 2016, 2021)                        ║
║  ─ NSE/BSE F&O Segment Rules & Bye-Laws                                    ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""
    print(disclaimer)
    log.info("[SEBI] Risk disclaimer displayed.")

