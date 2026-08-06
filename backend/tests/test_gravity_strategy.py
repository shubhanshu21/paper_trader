"""
tests/test_gravity_strategy.py — coverage for the pure signal/strike logic
in strategies/custom/gravity_strategy.py (the Camarilla fakeout-reversal
credit-spread engine). Direct-function-call style with a fake broker, no
real broker/DB — same pattern as tests/test_custom_strategy_scheduler_phase3.py.
Deliberately does NOT test enter()/close_leg()/_place() (those are thin
order-placement wrappers around BaseBroker, not decision logic).
"""
from datetime import date
from unittest.mock import MagicMock

import pytest

from strategies.custom.gravity_strategy import GravityStrategy


class FakeBroker:
    def __init__(self, candles=None, strike_step=50, lot_size=75):
        self.candles = candles or []
        self.strike_step = strike_step
        self.lot_size = lot_size
        self.dry_run = True

    def resolve_instrument_key(self, symbol):
        return f"KEY|{symbol}"

    def get_strike_step(self, symbol):
        return self.strike_step

    def get_lot_size(self, symbol):
        return self.lot_size

    def get_historical_candles(self, instrument_key, unit, interval, to_date):
        return self.candles


def make_strategy(candles=None, rules=None, strike_step=50, lot_size=75) -> GravityStrategy:
    broker = FakeBroker(candles=candles, strike_step=strike_step, lot_size=lot_size)
    default_rules = {
        "lots": 1, "expiry_offset": 0, "extreme_lookback_days": 10, "hedge_strikes_away": 2,
        "signal_check_time": "15:20", "target_credit_pct": 90, "min_roi_pct": 3,
        "exit_days_before_expiry": 2, "blackout_dates": [],
    }
    if rules:
        default_rules.update(rules)
    return GravityStrategy(
        broker=broker, audit=MagicMock(), kill_switch=MagicMock(), rate_limiter=MagicMock(),
        symbol="NIFTY", rules=default_rules, user_id=1,
    )


def candle(day: str, high: float, low: float, close: float) -> dict:
    return {"timestamp": f"{day}T00:00:00+0530", "high": high, "low": low, "close": close}


# ---------------------------------------------------------------------------
# _prev_month_ohlc
# ---------------------------------------------------------------------------
class TestPrevMonthOhlc:
    def test_extracts_high_low_and_last_close_of_prior_month(self):
        strategy = make_strategy()
        # Most-recent-first: today (Feb) then all of January.
        candles = [
            candle("2026-02-01", 105, 103, 104),
            candle("2026-01-30", 110, 100, 108),  # January's last trading day -> its close is prev month close
            candle("2026-01-15", 120, 95, 115),   # January's high/low extremes
            candle("2026-01-02", 108, 102, 106),
        ]
        result = strategy._prev_month_ohlc(candles)
        assert result == {"high": 120, "low": 95, "close": 108}

    def test_raises_when_only_current_month_history_exists(self):
        strategy = make_strategy()
        candles = [candle("2026-02-01", 105, 103, 104), candle("2026-02-02", 106, 104, 105)]
        with pytest.raises(RuntimeError, match="not enough daily history"):
            strategy._prev_month_ohlc(candles)


# ---------------------------------------------------------------------------
# evaluate_signal — BULLISH (S3 fakeout) / BEARISH (R3 fakeout) / no signal
# ---------------------------------------------------------------------------
class TestEvaluateSignal:
    def _jan_candles(self):
        # January: high=120, low=100, close=110 -> feeds Camarilla pivots for February evaluation.
        return [candle(f"2026-01-{d:02d}", 120, 100, 110) for d in range(2, 31)]

    def test_bullish_signal_when_yesterday_below_s3_and_today_closes_back_above(self):
        # rng = 20, s3 = 110 - 20*1.1/4 = 104.5
        jan = self._jan_candles()
        candles = [
            candle("2026-02-02", 106, 103, 105),  # today: closes back above S3 (104.5)
            candle("2026-02-01", 104, 99, 100),   # yesterday: closed below S3 (the fakeout)
            *jan,
        ]
        strategy = make_strategy(candles=candles)
        signal = strategy.evaluate_signal()
        assert signal is not None
        assert signal["signal"] == "BULLISH"
        assert signal["s3"] == pytest.approx(104.5)

    def test_bearish_signal_when_yesterday_above_r3_and_today_closes_back_below(self):
        # r3 = 110 + 20*1.1/4 = 115.5
        jan = self._jan_candles()
        candles = [
            candle("2026-02-02", 118, 112, 114),  # today: closes back below R3 (115.5)
            candle("2026-02-01", 119, 116, 118),  # yesterday: closed above R3 (the fakeout)
            *jan,
        ]
        strategy = make_strategy(candles=candles)
        signal = strategy.evaluate_signal()
        assert signal is not None
        assert signal["signal"] == "BEARISH"
        assert signal["r3"] == pytest.approx(115.5)

    def test_no_signal_when_price_stays_inside_the_band(self):
        jan = self._jan_candles()
        candles = [candle("2026-02-02", 111, 109, 110), candle("2026-02-01", 111, 109, 110), *jan]
        strategy = make_strategy(candles=candles)
        assert strategy.evaluate_signal() is None

    def test_no_signal_with_fewer_than_three_candles(self):
        strategy = make_strategy(candles=[candle("2026-02-02", 110, 108, 109), candle("2026-02-01", 110, 108, 109)])
        assert strategy.evaluate_signal() is None

    def test_extreme_is_the_lookback_window_low_for_bullish(self):
        jan = self._jan_candles()
        candles = [
            candle("2026-02-05", 106, 103, 105),
            candle("2026-02-04", 104, 90, 100),   # window's lowest low
            candle("2026-02-03", 104, 99, 100),
            candle("2026-02-02", 104, 99, 100),
            candle("2026-02-01", 104, 99, 100),
            *jan,
        ]
        strategy = make_strategy(candles=candles, rules={"extreme_lookback_days": 4})
        signal = strategy.evaluate_signal()
        assert signal["extreme"] == 90


# ---------------------------------------------------------------------------
# compute_spread_strikes — rounds AT-OR-BEYOND the extreme, never less OTM
# ---------------------------------------------------------------------------
class TestComputeSpreadStrikes:
    def test_bullish_floors_sold_strike_and_hedges_further_down(self):
        strategy = make_strategy(strike_step=50, rules={"hedge_strikes_away": 2})
        sold, hedge = strategy.compute_spread_strikes("BULLISH", extreme=24230)
        assert sold == 24200  # floor(24230/50)*50 -- at-or-beyond (below) the extreme
        assert hedge == 24100  # 2 strikes further OTM (lower)

    def test_bearish_ceils_sold_strike_and_hedges_further_up(self):
        strategy = make_strategy(strike_step=50, rules={"hedge_strikes_away": 2})
        sold, hedge = strategy.compute_spread_strikes("BEARISH", extreme=24230)
        assert sold == 24250  # ceil(24230/50)*50 -- at-or-beyond (above) the extreme
        assert hedge == 24350

    def test_extreme_exactly_on_a_strike_stays_put(self):
        strategy = make_strategy(strike_step=50, rules={"hedge_strikes_away": 1})
        sold, _ = strategy.compute_spread_strikes("BULLISH", extreme=24200)
        assert sold == 24200


# ---------------------------------------------------------------------------
# is_blacked_out
# ---------------------------------------------------------------------------
class TestIsBlackedOut:
    def test_true_inside_a_configured_window(self):
        strategy = make_strategy(rules={"blackout_dates": [{"start": "2026-03-01", "end": "2026-03-05"}]})
        assert strategy.is_blacked_out(today=date(2026, 3, 3)) is True

    def test_false_outside_any_window(self):
        strategy = make_strategy(rules={"blackout_dates": [{"start": "2026-03-01", "end": "2026-03-05"}]})
        assert strategy.is_blacked_out(today=date(2026, 3, 10)) is False

    def test_boundary_dates_are_inclusive(self):
        strategy = make_strategy(rules={"blackout_dates": [{"start": "2026-03-01", "end": "2026-03-05"}]})
        assert strategy.is_blacked_out(today=date(2026, 3, 1)) is True
        assert strategy.is_blacked_out(today=date(2026, 3, 5)) is True

    def test_false_with_no_windows_configured(self):
        strategy = make_strategy(rules={"blackout_dates": []})
        assert strategy.is_blacked_out(today=date(2026, 3, 3)) is False


# ---------------------------------------------------------------------------
# net_credit_and_max_loss
# ---------------------------------------------------------------------------
class TestNetCreditAndMaxLoss:
    def test_computes_credit_and_max_loss_for_a_put_spread(self):
        sold_leg = {"entry_price": 40.0, "strike": 24200, "quantity": 75}
        hedge_leg = {"entry_price": 15.0, "strike": 24100, "quantity": 75}
        net_credit, max_loss = GravityStrategy.net_credit_and_max_loss(sold_leg, hedge_leg)
        assert net_credit == pytest.approx(25.0 * 75)
        assert max_loss == pytest.approx(100 * 75 - 25.0 * 75)

    def test_handles_none_entry_prices_as_zero(self):
        sold_leg = {"entry_price": None, "strike": 24200, "quantity": 75}
        hedge_leg = {"entry_price": None, "strike": 24100, "quantity": 75}
        net_credit, max_loss = GravityStrategy.net_credit_and_max_loss(sold_leg, hedge_leg)
        assert net_credit == 0.0
        assert max_loss == pytest.approx(100 * 75)
