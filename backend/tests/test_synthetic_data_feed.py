"""
tests/test_synthetic_data_feed.py — coverage for the pure functions in
backtest/synthetic_data_feed.py (realized volatility, the synthetic
option token format, the weekly-expiry heuristic). Direct-function-call
style, no DB — same pattern as tests/test_zero_to_hero_strategy.py.
Deliberately does NOT test the DB-backed methods (preload/_spot_at/etc)
— those need a real MySQL fixture, out of scope for pure-logic coverage.
"""
import math
from datetime import date

from backtest.synthetic_data_feed import (
    _make_token,
    _next_weekly_expiries,
    _parse_option_token,
    realized_volatility,
)


class TestRealizedVolatility:
    def test_none_with_fewer_than_two_closes(self):
        assert realized_volatility([100.0]) is None
        assert realized_volatility([]) is None

    def test_zero_for_constant_prices(self):
        assert realized_volatility([100.0] * 10) == 0.0

    def test_positive_for_moving_prices(self):
        closes = [100, 102, 99, 105, 98, 107, 96, 110]
        sigma = realized_volatility(closes)
        assert sigma is not None
        assert sigma > 0

    def test_annualization_scales_with_sqrt_days(self):
        closes = [100, 101, 99, 102, 98]
        daily = realized_volatility(closes, annualization_days=1)
        annual = realized_volatility(closes, annualization_days=252)
        assert math.isclose(annual, daily * math.sqrt(252), rel_tol=1e-9)

    def test_skips_non_positive_prices_rather_than_crashing(self):
        # A zero/negative close shouldn't happen in real data, but this must
        # degrade gracefully (skip the bad transition) rather than raise on
        # log(0/x) — enough OTHER valid transitions remain here (3) that a
        # real sigma still comes out, unlike a file with too few good points.
        closes = [100, 101, 0, 102, 105, 103]
        sigma = realized_volatility(closes)
        assert sigma is not None


class TestTokenRoundTrip:
    def test_make_then_parse_round_trips(self):
        token = _make_token("NIFTY", "2024-01-25", 21600, "PE")
        assert _parse_option_token(token) == ("NIFTY", "2024-01-25", 21600, "PE")

    def test_non_token_string_returns_none(self):
        assert _parse_option_token("NSE_INDEX|Nifty 50") is None
        assert _parse_option_token("BHAV|NIFTY|2024-01-25|21600|PE") is None  # different prefix, not ours

    def test_malformed_token_returns_none(self):
        assert _parse_option_token("SYN|NIFTY|21600|PE") is None  # missing a field


class TestNextWeeklyExpiries:
    def test_from_a_thursday_returns_that_thursday_first(self):
        # 2026-08-13 is a Thursday.
        result = _next_weekly_expiries(date(2026, 8, 13), count=1)
        assert result == [date(2026, 8, 13)]

    def test_from_a_friday_rolls_to_next_thursday(self):
        # 2026-08-07 is a Friday.
        result = _next_weekly_expiries(date(2026, 8, 7), count=1)
        assert result == [date(2026, 8, 13)]

    def test_returns_count_consecutive_weekly_expiries(self):
        result = _next_weekly_expiries(date(2026, 8, 7), count=3)
        assert result == [date(2026, 8, 13), date(2026, 8, 20), date(2026, 8, 27)]
