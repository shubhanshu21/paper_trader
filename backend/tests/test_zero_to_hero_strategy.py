"""
tests/test_zero_to_hero_strategy.py — coverage for the pure signal logic
in strategies/custom/zero_to_hero_strategy.py (the PDH/PDL breakout-
pullback option-buying engine) and the rules contract in
zero_to_hero_schema.py. Direct-function-call style with a fake broker,
no real broker/DB — same pattern as tests/test_gravity_strategy.py.
Deliberately does NOT test enter()/resolve_leg() (thin order-placement
wrappers around BaseBroker, not decision logic).
"""
from unittest.mock import MagicMock

from strategies.custom.zero_to_hero_schema import (
    describe_zero_to_hero_rules,
    validate_zero_to_hero_rules,
)
from strategies.custom.zero_to_hero_strategy import (
    ZeroToHeroStrategy,
    _detect_entry_pattern,
)


class FakeBroker:
    def __init__(self, daily_candles=None, intraday_candles=None, ltp=None, strike_step=50, lot_size=75):
        self.daily_candles = daily_candles or []
        self.intraday_candles = intraday_candles or []
        self.ltp = ltp
        self.strike_step = strike_step
        self.lot_size = lot_size
        self.dry_run = True

    def resolve_instrument_key(self, symbol):
        return f"KEY|{symbol}"

    def get_current_time(self):
        return None  # matches BaseBroker's default contract — None means "use real wall-clock date.today()"

    def get_strike_step(self, symbol):
        return self.strike_step

    def get_lot_size(self, symbol):
        return self.lot_size

    def get_ltp(self, instrument_key):
        return self.ltp

    def get_historical_candles(self, instrument_key, unit, interval, to_date):
        return self.daily_candles if unit == "days" else self.intraday_candles


def make_strategy(daily_candles=None, intraday_candles=None, ltp=None, rules=None) -> ZeroToHeroStrategy:
    broker = FakeBroker(daily_candles=daily_candles, intraday_candles=intraday_candles, ltp=ltp)
    default_rules = {
        "lots": 2, "candle_interval_minutes": 15, "sl_buffer_points": 5, "max_pullback_candles": 2,
        "expiry_offset": 0, "exit_time": "15:15", "max_reentries": 1,
    }
    if rules:
        default_rules.update(rules)
    return ZeroToHeroStrategy(
        broker=broker, audit=MagicMock(), kill_switch=MagicMock(), rate_limiter=MagicMock(),
        symbol="NIFTY", rules=default_rules, user_id=1,
    )


def candle(ts: str, o: float, h: float, low: float, c: float) -> dict:
    return {"timestamp": ts, "open": o, "high": h, "low": low, "close": c}


TODAY = "2026-08-07"
YDAY = "2026-08-06"


# ---------------------------------------------------------------------------
# get_previous_day_levels
# ---------------------------------------------------------------------------
class TestGetPreviousDayLevels:
    def test_returns_prev_session_high_low(self):
        daily = [
            candle(f"{TODAY}T00:00:00+0530", 100, 110, 95, 108),
            candle(f"{YDAY}T00:00:00+0530", 90, 105, 85, 100),
        ]
        strat = make_strategy(daily_candles=daily)
        levels = strat.get_previous_day_levels()
        assert levels == {"pdh": 105, "pdl": 85}

    def test_none_with_fewer_than_two_sessions(self):
        strat = make_strategy(daily_candles=[candle(f"{TODAY}T00:00:00+0530", 100, 110, 95, 108)])
        assert strat.get_previous_day_levels() is None


# ---------------------------------------------------------------------------
# _detect_entry_pattern (the core pullback/confirm scan)
# ---------------------------------------------------------------------------
class TestDetectEntryPattern:
    def test_bearish_single_candle_pullback_confirms(self):
        # PDL break, one green pullback candle, then a red candle closing below that green candle's open.
        candles = [
            candle("t0", 100, 101, 95, 96),   # red, establishes the break
            candle("t1", 96, 99, 95, 98),      # green pullback (1 candle)
            candle("t2", 97, 98, 90, 91),      # red confirm, closes (91) below t1's open (96)
        ]
        result = _detect_entry_pattern(candles, "BEARISH", max_pullback=2)
        assert result is not None
        assert result["entry_candle"] == candles[-1]
        assert result["pullback_len"] == 1

    def test_bullish_two_candle_pullback_confirms(self):
        candles = [
            candle("t0", 100, 105, 99, 104),   # green, establishes the break
            candle("t1", 104, 105, 101, 102),  # red pullback #1
            candle("t2", 102, 103, 100, 101),  # red pullback #2
            candle("t3", 101, 108, 100, 107),  # green confirm, closes (107) above t2's open (102)
        ]
        result = _detect_entry_pattern(candles, "BULLISH", max_pullback=2)
        assert result is not None
        assert result["pullback_len"] == 2

    def test_three_candle_pullback_cancels_setup(self):
        candles = [
            candle("t0", 100, 101, 95, 96),
            candle("t1", 96, 99, 95, 98),
            candle("t2", 98, 100, 96, 99),
            candle("t3", 99, 101, 97, 100),  # 3 green candles in a row now
            candle("t4", 100, 101, 90, 91),  # red confirm — but pullback run is 3, not 1-2
        ]
        assert _detect_entry_pattern(candles, "BEARISH", max_pullback=2) is None

    def test_confirm_candle_wrong_color_returns_none(self):
        candles = [
            candle("t0", 100, 101, 95, 96),
            candle("t1", 96, 99, 95, 98),  # green pullback
            candle("t2", 98, 100, 97, 99),  # still green — no red confirm yet
        ]
        assert _detect_entry_pattern(candles, "BEARISH", max_pullback=2) is None

    def test_confirm_close_not_beyond_reference_open_returns_none(self):
        candles = [
            candle("t0", 100, 101, 95, 96),
            candle("t1", 96, 99, 95, 98),   # green pullback, open=96
            candle("t2", 97, 98, 96, 97),   # red, but closes (97) above t1's open (96) — no real breach
        ]
        assert _detect_entry_pattern(candles, "BEARISH", max_pullback=2) is None

    def test_no_pullback_candle_returns_none(self):
        # Confirm-color candle immediately follows another confirm-color candle — zero-length pullback.
        candles = [
            candle("t0", 100, 101, 95, 96),
            candle("t1", 96, 97, 90, 91),
        ]
        assert _detect_entry_pattern(candles, "BEARISH", max_pullback=2) is None


# ---------------------------------------------------------------------------
# evaluate_signal (wires PDH/PDL + candles + pattern together)
# ---------------------------------------------------------------------------
class TestEvaluateSignal:
    def _daily(self):
        return [
            candle(f"{TODAY}T00:00:00+0530", 100, 110, 95, 108),
            candle(f"{YDAY}T00:00:00+0530", 90, 105, 85, 100),  # PDH=105, PDL=85
        ]

    def test_no_trade_zone_returns_none_signal(self):
        intraday = [
            candle(f"{TODAY}T09:15:00+0530", 100, 101, 99, 100),  # inside 85-105 band
            candle(f"{TODAY}T09:30:00+0530", 100, 102, 98, 101),
        ]
        strat = make_strategy(daily_candles=self._daily(), intraday_candles=intraday, ltp=101)
        assert strat.evaluate_signal() == {"signal": "NONE"}

    def test_bearish_breakout_and_confirmed_pullback_returns_full_signal(self):
        intraday = [
            candle(f"{TODAY}T09:15:00+0530", 90, 91, 80, 81),   # closes (81) below PDL=85 — bias established
            candle(f"{TODAY}T09:30:00+0530", 81, 84, 80, 83),   # green pullback, open=81
            candle(f"{TODAY}T09:45:00+0530", 83, 84, 75, 78),   # red confirm, closes (78) below 81
        ]
        strat = make_strategy(daily_candles=self._daily(), intraday_candles=intraday, ltp=78)
        signal = strat.evaluate_signal()
        assert signal["signal"] == "BEARISH"
        assert signal["option_type"] == "PE"
        assert signal["entry_index_price"] == 78
        # SL is beyond the entry (confirm) candle's HIGH plus buffer, target is equidistant on the other side.
        assert signal["sl_index_price"] == 84 + 5
        risk = signal["sl_index_price"] - 78
        assert signal["target_index_price"] == 78 - risk
        assert signal["trigger_timestamp"] == f"{TODAY}T09:45:00+0530"

    def test_bullish_breakout_no_pullback_yet_returns_none_signal(self):
        intraday = [
            candle(f"{TODAY}T09:15:00+0530", 105, 112, 104, 111),  # closes above PDH=105
            candle(f"{TODAY}T09:30:00+0530", 111, 113, 109, 112),  # still green — no pullback/confirm yet
        ]
        strat = make_strategy(daily_candles=self._daily(), intraday_candles=intraday, ltp=112)
        assert strat.evaluate_signal() == {"signal": "NONE"}


# ---------------------------------------------------------------------------
# schema validation / description
# ---------------------------------------------------------------------------
class TestValidateRules:
    def test_valid_rules_pass(self):
        rules = {
            "lots": 2, "candle_interval_minutes": 15, "sl_buffer_points": 5,
            "max_pullback_candles": 2, "expiry_offset": 0, "exit_time": "15:15", "max_reentries": 1,
        }
        assert validate_zero_to_hero_rules(rules) == []

    def test_odd_lots_rejected(self):
        errors = validate_zero_to_hero_rules({"lots": 3})
        assert any("even" in e for e in errors)

    def test_single_lot_rejected(self):
        errors = validate_zero_to_hero_rules({"lots": 0})
        assert any("even" in e for e in errors)

    def test_bad_exit_time_rejected(self):
        errors = validate_zero_to_hero_rules({"lots": 2, "exit_time": "3:15pm"})
        assert any("exit_time" in e for e in errors)

    def test_non_dict_rejected(self):
        assert validate_zero_to_hero_rules([]) == ["Strategy rules must be an object."]


class TestDescribeRules:
    def test_empty_rules_message(self):
        assert describe_zero_to_hero_rules(None) == "No rules configured yet."

    def test_includes_symbol_and_key_settings(self):
        text = describe_zero_to_hero_rules({"lots": 4, "exit_time": "15:15", "max_reentries": 1}, symbol="NIFTY")
        assert "NIFTY" in text
        assert "4 lots" in text
        assert "15:15" in text
