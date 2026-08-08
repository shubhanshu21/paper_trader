"""
tests/test_technical_indicators.py — pure-function tests for the RSI and
Bollinger Bands additions to utils/technical_indicators.py (added to back
the RSI/BOLLINGER_WIDTH custom-strategy entry conditions). No DB, no
network — hand-computed/known-shape expected values on small series.
"""
from utils.technical_indicators import bollinger_bands, rsi


class TestRSI:
    def test_none_until_period_plus_one_values(self):
        values = [100.0, 101.0, 102.0]
        out = rsi(values, period=5)
        assert out == [None, None, None]

    def test_all_gains_gives_rsi_100(self):
        values = [100.0 + i for i in range(10)]  # strictly increasing
        out = rsi(values, period=5)
        assert out[5] == 100.0
        assert out[-1] == 100.0

    def test_all_losses_gives_rsi_0(self):
        values = [110.0 - i for i in range(10)]  # strictly decreasing
        out = rsi(values, period=5)
        assert out[5] == 0.0

    def test_flat_series_gives_rsi_100_no_divide_by_zero(self):
        # No losses at all (avg_loss == 0) -> defined as 100, not a crash.
        values = [100.0] * 10
        out = rsi(values, period=5)
        assert out[5] == 100.0

    def test_output_length_matches_input(self):
        values = [100.0 + (i % 3) for i in range(20)]
        out = rsi(values, period=7)
        assert len(out) == len(values)

    def test_raises_on_invalid_period(self):
        try:
            rsi([1.0, 2.0], period=0)
            raise AssertionError("expected ValueError")
        except ValueError:
            pass


class TestBollingerBands:
    def test_none_before_period_closes_exist(self):
        values = [100.0, 101.0, 102.0]
        out = bollinger_bands(values, period=5)
        assert out == [None, None, None]

    def test_flat_series_has_zero_width(self):
        values = [100.0] * 20
        out = bollinger_bands(values, period=10)
        latest = out[-1]
        assert latest["middle"] == 100.0
        assert latest["upper"] == 100.0
        assert latest["lower"] == 100.0
        assert latest["width"] == 0.0

    def test_volatile_series_has_positive_width(self):
        values = [100.0, 110.0, 90.0, 105.0, 95.0, 108.0, 92.0, 103.0, 97.0, 106.0]
        out = bollinger_bands(values, period=10)
        latest = out[-1]
        assert latest["width"] > 0
        assert latest["lower"] < latest["middle"] < latest["upper"]

    def test_output_length_matches_input(self):
        values = [100.0 + (i % 4) for i in range(25)]
        out = bollinger_bands(values, period=12)
        assert len(out) == len(values)

    def test_raises_on_invalid_period(self):
        try:
            bollinger_bands([1.0, 2.0], period=1)
            raise AssertionError("expected ValueError")
        except ValueError:
            pass
