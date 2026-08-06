"""
tests/test_otm_put_roll_strategy.py — coverage for the pure candle-based
pullback-detection logic in strategies/custom/otm_put_roll_strategy.py
(the far-month OTM put roll-down engine). Direct-function-call style with
a fake broker, no real broker/DB — same pattern as
tests/test_gravity_strategy.py.
"""
from unittest.mock import MagicMock

import pytest

from strategies.custom.otm_put_roll_strategy import OtmPutRollStrategy


class FakeBroker:
    def __init__(self, strike_step=50, lot_size=75, minute_candles=None, daily_candles=None):
        self.strike_step = strike_step
        self.lot_size = lot_size
        self.minute_candles = minute_candles or []
        self.daily_candles = daily_candles or []
        self.dry_run = True

    def resolve_instrument_key(self, symbol):
        return f"KEY|{symbol}"

    def get_strike_step(self, symbol):
        return self.strike_step

    def get_lot_size(self, symbol):
        return self.lot_size

    def get_historical_candles(self, instrument_key, unit, interval, to_date):
        return self.minute_candles if unit == "minutes" else self.daily_candles


def make_strategy(rules=None, strike_step=50, lot_size=75, minute_candles=None, daily_candles=None) -> OtmPutRollStrategy:
    broker = FakeBroker(strike_step=strike_step, lot_size=lot_size, minute_candles=minute_candles, daily_candles=daily_candles)
    default_rules = {
        "lots": 1, "initial_otm_points": 500, "expiry_offset": 1, "pullback_lookback_days": 20,
        "pullback_min_points": 500, "roll_points": 200, "max_rolls_per_cycle": 8,
        "candle_interval_minutes": 60, "target_capital_pct": 5, "exit_days_before_expiry": 1,
    }
    if rules:
        default_rules.update(rules)
    return OtmPutRollStrategy(
        broker=broker, audit=MagicMock(), kill_switch=MagicMock(), rate_limiter=MagicMock(),
        symbol="NIFTY", rules=default_rules, user_id=1,
    )


def candle(high, low, close):
    return {"high": high, "low": low, "close": close}


# ---------------------------------------------------------------------------
# confirmed_close — latest CLOSED candle, never a raw tick
# ---------------------------------------------------------------------------
class TestConfirmedClose:
    def test_returns_most_recent_candles_close(self):
        # most-recent-first per BaseBroker's contract
        strategy = make_strategy(minute_candles=[candle(24300, 24250, 24280), candle(24250, 24200, 24230)])
        assert strategy.confirmed_close() == 24280

    def test_raises_with_no_candles(self):
        strategy = make_strategy(minute_candles=[])
        with pytest.raises(RuntimeError, match="no 60-minute candles"):
            strategy.confirmed_close()


# ---------------------------------------------------------------------------
# recent_high — max daily HIGH over the lookback window
# ---------------------------------------------------------------------------
class TestRecentHigh:
    def test_max_high_within_lookback_window(self):
        daily = [candle(24500, 24300, 24400), candle(24800, 24600, 24700), candle(24600, 24400, 24500)]
        strategy = make_strategy(rules={"pullback_lookback_days": 3}, daily_candles=daily)
        assert strategy.recent_high() == 24800

    def test_ignores_candles_outside_the_lookback_window(self):
        daily = [candle(24500, 24300, 24400), candle(25500, 25300, 25400)]  # 2nd candle (higher) is outside a 1-day window
        strategy = make_strategy(rules={"pullback_lookback_days": 1}, daily_candles=daily)
        assert strategy.recent_high() == 24500

    def test_raises_with_no_daily_candles(self):
        strategy = make_strategy(daily_candles=[])
        with pytest.raises(RuntimeError, match="no daily candles"):
            strategy.recent_high()


# ---------------------------------------------------------------------------
# pullback_points
# ---------------------------------------------------------------------------
class TestPullbackPoints:
    def test_computes_points_off_the_recent_high(self):
        strategy = make_strategy(
            rules={"pullback_lookback_days": 2},
            minute_candles=[candle(24310, 24280, 24300)],
            daily_candles=[candle(24800, 24600, 24700), candle(24500, 24300, 24400)],
        )
        pullback, close = strategy.pullback_points()
        assert close == 24300
        assert pullback == pytest.approx(24800 - 24300)

    def test_zero_or_negative_pullback_when_price_is_at_or_above_the_high(self):
        strategy = make_strategy(
            minute_candles=[candle(24810, 24790, 24800)],
            daily_candles=[candle(24800, 24600, 24700)],
        )
        pullback, _close = strategy.pullback_points()
        assert pullback <= 0  # price at/above the recent high -> no real pullback yet
