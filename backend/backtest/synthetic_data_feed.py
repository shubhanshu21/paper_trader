"""
backtest/synthetic_data_feed.py — DataFeed backed by real 1-minute
UNDERLYING candles (db.models.Index1MinCandle — see scripts/
import_kaggle_index_candles.py for where those came from), with option
prices THEORETICALLY RECONSTRUCTED via Black-76 (utils/black76.py)
rather than read from any real historical option quote — there is no
real historical option price data in this app (see that import script's
own docstring: that's a separate, still-unsolved sourcing problem).

This is a deliberate, clearly-labeled APPROXIMATION, not real market
data, on four fronts:

  1. Volatility: no real historical IV exists here either, so `sigma` is
     the underlying's own trailing REALIZED volatility (close-to-close
     log returns, annualized) — a standard, defensible proxy, but it is
     NOT real implied vol (misses skew/smile, term structure, and any
     IV expansion ahead of a known event that realized vol can't see
     coming). See realized_volatility()'s own docstring.
  2. Forward price: F is approximated as the spot underlying price with
     no cost-of-carry adjustment — fine for index options at the short
     DTEs these strategies actually trade, measurably wrong for a
     far-dated contract.
  3. Strike grid / lot size: hardcoded to today's real NSE values (50pt
     NIFTY / 100pt BANKNIFTY strikes, current lot sizes) applied
     uniformly across the whole backtest — both have actually changed
     multiple times over the real history this table covers (e.g. NIFTY
     lot size was 50 before 2024). A period-accurate lookup is a real
     gap, not modeled here.
  4. Expiry calendar: nearest-Thursday-forward is used as a WEEKLY expiry
     heuristic — NSE's real expiry weekday has changed more than once
     over this table's history (Thursday historically, moved for some
     symbols since). Not a period-accurate historical calendar.

Good enough to validate a strategy's SIGNAL logic (does the entry
trigger fire when it should, does the exit timing work) against real
price action. NOT good enough to trust the resulting P&L numbers as
"this is what you'd have really made" — report results as directional,
not literal.
"""
import math
from bisect import bisect_right
from datetime import date, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import text
from sqlalchemy.orm import Session

from utils.black76 import DEFAULT_RISK_FREE_RATE, black76_price
from utils.logger import get_logger

log = get_logger(__name__)

_TOKEN_PREFIX = "SYN"

# Real current NSE values, applied uniformly across the whole backtest —
# see module docstring point 3 on why this isn't period-accurate.
_STRIKE_STEP = {"NIFTY": 50, "BANKNIFTY": 100}
_LOT_SIZE = {"NIFTY": 75, "BANKNIFTY": 30}
_STRIKE_RANGE = 15  # how many strikes on each side of spot the synthetic chain offers


def _make_token(symbol: str, expiry: str, strike: int, option_type: str) -> str:
    return f"{_TOKEN_PREFIX}|{symbol}|{expiry}|{strike}|{option_type}"


def _parse_option_token(token: str) -> tuple[str, str, int, str] | None:
    parts = token.split("|")
    if len(parts) != 5 or parts[0] != _TOKEN_PREFIX:
        return None
    _, symbol, expiry, strike, option_type = parts
    return symbol, expiry, int(strike), option_type


def realized_volatility(closes: list[float], annualization_days: int = 252) -> float | None:
    """
    Annualized close-to-close realized volatility from a chronological
    list of daily closes — stdlib-only (no numpy dependency), same
    "pure function over plain data" discipline as utils/technical_
    indicators.py. Returns None if there aren't at least 2 closes (can't
    compute a single return, let alone a stdev).

    This is a PROXY for implied volatility, not IV itself — see this
    module's docstring point 1.
    """
    if len(closes) < 2:
        return None
    log_returns = []
    for i in range(1, len(closes)):
        prev, cur = closes[i - 1], closes[i]
        if prev <= 0 or cur <= 0:
            continue
        log_returns.append(math.log(cur / prev))
    if len(log_returns) < 2:
        return None
    mean = sum(log_returns) / len(log_returns)
    variance = sum((r - mean) ** 2 for r in log_returns) / (len(log_returns) - 1)
    return math.sqrt(variance) * math.sqrt(annualization_days)


def _next_weekly_expiries(from_date: date, count: int) -> list[date]:
    """Nearest Thursday >= from_date, then +7 days each — see module docstring point 4 on why this is a heuristic, not a real historical calendar."""
    days_until_thursday = (3 - from_date.weekday()) % 7  # Monday=0 .. Thursday=3
    first = from_date + timedelta(days=days_until_thursday)
    return [first + timedelta(weeks=i) for i in range(count)]


class SyntheticOptionDataFeed:
    """
    Args:
        session:          SQLAlchemy session open against the app's DB (index_1min_candles).
        symbol:            'NIFTY' | 'BANKNIFTY'.
        risk_free_rate:    Defaults to utils.black76.DEFAULT_RISK_FREE_RATE.
        vol_lookback_days: Trailing daily-close window realized_volatility() is computed over. Default 20 (~1 trading month).
    """

    def __init__(
        self, session: Session, symbol: str,
        risk_free_rate: float = DEFAULT_RISK_FREE_RATE, vol_lookback_days: int = 20,
    ) -> None:
        self.session = session
        self.symbol = symbol.upper()
        self.risk_free_rate = risk_free_rate
        self.vol_lookback_days = vol_lookback_days
        self.equity_key = f"SPOT|{self.symbol}"
        self.current_time: datetime | None = None

        # ALL data access below reads from this in-memory list, loaded
        # ONCE by preload() for the whole backtest range — not a DB query
        # per simulated minute. A naive per-tick-query design (the first
        # version of this class) made a 10-day backtest take minutes:
        # every tick needs candles + PDH/PDL + a spot check + realized
        # vol, and a run replays every real market minute in the range
        # (hundreds of thousands for a multi-month run) — that's
        # hundreds of thousands of round-trips, not tens.
        self._candles: list[dict] = []       # chronological, [{"ts": datetime, "open"/"high"/"low"/"close": float}, ...]
        self._candle_ts: list[datetime] = []  # parallel list of just the ts, for bisect
        self._daily_closes: list[tuple[date, float]] = []  # one (date, last close) per session, chronological
        self._vol_cache: dict[date, float | None] = {}      # realized vol barely moves within a day — cache it

        if self.symbol not in _STRIKE_STEP:
            raise ValueError(f"SyntheticOptionDataFeed only supports {sorted(_STRIKE_STEP)}, got '{symbol}'.")

    def preload(self, start: datetime, end: datetime) -> None:
        """
        Load every real minute candle for [start, end] into memory in a
        SINGLE query — call once per backtest run (or per chunk of days,
        if the range is large enough that holding it all in memory is a
        real concern) before replaying any ticks. `start` should already
        include enough lookback buffer for both PDH/PDL (1 extra session)
        and realized-vol (vol_lookback_days extra sessions) — see
        backtest/synthetic_engine.py's own call site for the actual buffer.
        """
        rows = self.session.execute(
            text("SELECT ts, open, high, low, close FROM index_1min_candles WHERE symbol = :symbol AND ts >= :start AND ts <= :end ORDER BY ts ASC"),
            {"symbol": self.symbol, "start": start, "end": end},
        ).fetchall()
        self._candles = [{"ts": r[0], "open": float(r[1]), "high": float(r[2]), "low": float(r[3]), "close": float(r[4])} for r in rows]
        self._candle_ts = [c["ts"] for c in self._candles]

        daily: dict[date, float] = {}
        for c in self._candles:
            daily[c["ts"].date()] = c["close"]  # chronological iteration — last write per date wins, i.e. that session's last close
        self._daily_closes = sorted(daily.items())

    def set_time(self, new_time: datetime) -> None:
        self.current_time = new_time

    # ------------------------------------------------------------------
    # Underlying
    # ------------------------------------------------------------------
    def get_ltp(self, instrument_key: str) -> float | None:
        # Any key that ISN'T one of our own "SYN|..." option tokens is
        # treated as this feed's one underlying, whatever exact string
        # produced it (MockBroker.resolve_instrument_key() delegates to
        # the real InstrumentCache, e.g. "NSE_INDEX|Nifty 50" — not the
        # `self.equity_key` placeholder this feed picked) — a feed
        # instance only ever serves ONE symbol, so there's no real
        # ambiguity to resolve here.
        parsed = _parse_option_token(instrument_key)
        if parsed is None:
            return self._spot_at(self.current_time)
        _symbol, expiry_str, strike, option_type = parsed
        return self._synthetic_price(expiry_str, strike, option_type)

    def _spot_at(self, at_time: datetime | None) -> float | None:
        if at_time is None or not self._candle_ts:
            return None
        idx = bisect_right(self._candle_ts, at_time) - 1
        if idx < 0:
            return None
        return self._candles[idx]["close"]

    def get_historical_candles(self, instrument_key: str, unit: str, interval: int, to_date: str) -> list[dict] | None:
        """
        Real minute candles from the preloaded in-memory window,
        resampled to `interval` minutes (unit='minutes') or to daily bars
        (unit='days') — same return shape BaseBroker.get_historical_candles
        documents. Served from memory (see preload()), not a DB query —
        this gets called on every simulated minute the backtest replay
        loop is scanning for an entry.
        """
        if _parse_option_token(instrument_key) is not None or not self._candle_ts:
            return None  # candles are only meaningful for the underlying, never a synthetic option token
        # `to_date` is a bare DATE string ("2024-01-03") — datetime.fromisoformat()
        # on that is MIDNIGHT, which would exclude every candle of that
        # date's own session (all of which are AFTER 00:00). The real
        # broker's contract is "up through now" for the current/live day
        # (there ARE no future candles to accidentally leak); the
        # simulated equivalent of "now" is self.current_time, which is
        # what this must bound by whenever to_date is today's date —
        # otherwise evaluate_signal() silently sees zero of today's
        # candles on every single tick, which is exactly the bug this
        # comment is here to prevent regressing.
        requested_date = date.fromisoformat(to_date)
        if self.current_time is not None and self.current_time.date() == requested_date:
            to_dt = self.current_time
        else:
            to_dt = datetime.combine(requested_date, datetime.max.time())
        from_dt = to_dt - timedelta(days=10)
        lo = bisect_right(self._candle_ts, from_dt)
        hi = bisect_right(self._candle_ts, to_dt)
        window = self._candles[lo:hi]
        if not window:
            return None
        minute_candles = [{"timestamp": c["ts"].isoformat(), "open": c["open"], "high": c["high"], "low": c["low"], "close": c["close"]} for c in window]

        if unit == "minutes":
            return _resample_minutes(minute_candles, interval)
        if unit == "days":
            return _resample_daily(minute_candles)
        return None

    # ------------------------------------------------------------------
    # Options (synthetic)
    # ------------------------------------------------------------------
    def get_option_contracts(self, instrument_key: str) -> list[str]:
        if _parse_option_token(instrument_key) is not None or self.current_time is None:
            return []
        return [d.isoformat() for d in _next_weekly_expiries(self.current_time.date(), count=6)]

    def get_option_chain(self, instrument_key: str, expiry_date: str) -> list[SimpleNamespace]:
        if _parse_option_token(instrument_key) is not None:
            return []
        spot = self._spot_at(self.current_time)
        if spot is None:
            return []
        step = _STRIKE_STEP[self.symbol]
        atm = round(spot / step) * step
        chain = []
        for i in range(-_STRIKE_RANGE, _STRIKE_RANGE + 1):
            strike = int(atm + i * step)
            chain.append(SimpleNamespace(
                strike_price=strike,
                call_options=SimpleNamespace(instrument_key=_make_token(self.symbol, expiry_date, strike, "CE")),
                put_options=SimpleNamespace(instrument_key=_make_token(self.symbol, expiry_date, strike, "PE")),
            ))
        return chain

    def _synthetic_price(self, expiry_str: str, strike: int, option_type: str) -> float | None:
        spot = self._spot_at(self.current_time)
        if spot is None or self.current_time is None:
            return None
        expiry_dt = datetime.fromisoformat(expiry_str).replace(hour=15, minute=30)
        seconds_to_expiry = (expiry_dt - self.current_time).total_seconds()
        if seconds_to_expiry <= 0:
            return max(spot - strike, 0.0) if option_type == "CE" else max(strike - spot, 0.0)
        T = seconds_to_expiry / (365 * 24 * 3600)

        sigma = self._trailing_realized_vol()
        if sigma is None or sigma <= 0:
            sigma = 0.15  # last-resort flat fallback (roughly NIFTY's long-run average IV) if there isn't enough daily history yet to compute one

        try:
            return round(black76_price(F=spot, K=float(strike), T=T, r=self.risk_free_rate, sigma=sigma, option_type=option_type), 2)
        except ValueError:
            return None

    def _trailing_realized_vol(self) -> float | None:
        """Cached per calendar date (see __init__) — this barely changes within a single day, and gets called on every option-price lookup. Served from the preloaded in-memory daily-close series, not a DB query."""
        if self.current_time is None or not self._daily_closes:
            return None
        today = self.current_time.date()
        if today in self._vol_cache:
            return self._vol_cache[today]

        dates = [d for d, _ in self._daily_closes]
        idx = bisect_right(dates, today)  # first session AFTER today — everything before it is eligible
        window = self._daily_closes[max(0, idx - (self.vol_lookback_days + 1)):idx]
        sigma = realized_volatility([c for _, c in window])
        self._vol_cache[today] = sigma
        return sigma

    # ------------------------------------------------------------------
    # Misc BaseBroker-shaped helpers
    # ------------------------------------------------------------------
    def get_current_time(self) -> datetime | None:
        return self.current_time

    def resolve_instrument_key(self, symbol: str) -> str:
        return f"SPOT|{symbol.upper()}"

    def get_lot_size(self, symbol: str) -> int | None:
        return _LOT_SIZE.get(symbol.upper())

    def get_strike_step(self, symbol: str) -> float | None:
        return _STRIKE_STEP.get(symbol.upper())


def _resample_minutes(minute_candles: list[dict], interval: int) -> list[dict]:
    """Group consecutive 1-min candles into `interval`-minute bars, bucketed by (date, minute-of-day // interval) so a bucket never spans a session boundary."""
    if interval <= 1:
        return minute_candles
    buckets: dict[tuple, list[dict]] = {}
    for c in minute_candles:
        ts = datetime.fromisoformat(c["timestamp"])
        minute_of_day = ts.hour * 60 + ts.minute
        key = (ts.date(), minute_of_day // interval)
        buckets.setdefault(key, []).append(c)
    out = []
    for key in sorted(buckets):
        group = buckets[key]
        out.append({
            "timestamp": group[0]["timestamp"], "open": group[0]["open"],
            "high": max(g["high"] for g in group), "low": min(g["low"] for g in group),
            "close": group[-1]["close"],
        })
    return out


def _resample_daily(minute_candles: list[dict]) -> list[dict]:
    buckets: dict[date, list[dict]] = {}
    for c in minute_candles:
        ts = datetime.fromisoformat(c["timestamp"])
        buckets.setdefault(ts.date(), []).append(c)
    out = []
    for d in sorted(buckets):
        group = buckets[d]
        out.append({
            "timestamp": d.isoformat(), "open": group[0]["open"],
            "high": max(g["high"] for g in group), "low": min(g["low"] for g in group),
            "close": group[-1]["close"],
        })
    return out
