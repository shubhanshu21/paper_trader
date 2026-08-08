"""
tests/test_custom_engine_conditions.py — unit tests for
CustomRuleBacktestEngine's equity-side RSI/Bollinger-width entry condition
methods (backtest/custom_engine.py). These are pure functions of their
arguments (no `self` state read) so they're called directly on the class
with a dummy `self`, avoiding the DB/InstrumentCache setup a full engine
construction would need — same "independently testable" discipline as
utils/technical_indicators.py itself.
"""
from backtest.custom_engine import CustomRuleBacktestEngine as Engine


class TestEquityRSICondition:
    def test_not_enough_history_returns_false(self):
        closes = [100.0, 101.0]
        assert Engine._equity_rsi_condition_met(None, closes, period=14, operator="BELOW", threshold=30, today_price=102.0) is False

    def test_strong_uptrend_triggers_above(self):
        closes = [90.0 + i for i in range(14)]  # strictly increasing
        assert Engine._equity_rsi_condition_met(None, closes, period=14, operator="ABOVE", threshold=70, today_price=110.0) is True

    def test_strong_downtrend_triggers_below(self):
        closes = [120.0 - i for i in range(14)]  # strictly decreasing
        assert Engine._equity_rsi_condition_met(None, closes, period=14, operator="BELOW", threshold=30, today_price=100.0) is True

    def test_invalid_period_returns_false(self):
        closes = [100.0] * 20
        assert Engine._equity_rsi_condition_met(None, closes, period=1, operator="BELOW", threshold=30, today_price=100.0) is False


class TestEquityBollingerWidthCondition:
    def test_not_enough_history_returns_false(self):
        closes = [100.0, 101.0]
        assert Engine._equity_bollinger_width_condition_met(None, closes, period=20, operator="BELOW", threshold=0.05, today_price=102.0) is False

    def test_flat_series_has_near_zero_width_triggers_squeeze(self):
        closes = [100.0] * 19
        assert Engine._equity_bollinger_width_condition_met(None, closes, period=20, operator="BELOW", threshold=0.01, today_price=100.0) is True

    def test_volatile_series_does_not_trigger_squeeze(self):
        closes = [100.0, 130.0, 70.0, 120.0, 80.0, 125.0, 75.0, 118.0, 82.0, 122.0,
                   78.0, 116.0, 84.0, 120.0, 80.0, 119.0, 81.0, 117.0, 83.0]
        assert Engine._equity_bollinger_width_condition_met(None, closes, period=20, operator="BELOW", threshold=0.02, today_price=110.0) is False

    def test_invalid_period_returns_false(self):
        closes = [100.0] * 20
        assert Engine._equity_bollinger_width_condition_met(None, closes, period=1, operator="BELOW", threshold=0.05, today_price=100.0) is False
