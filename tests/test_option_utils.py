"""
tests/test_option_utils.py — Pure strike-math tests for utils/option_utils.py.
No network, no broker, no credentials needed.
"""
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from automate.utils.option_utils import (
    calculate_strangle_strikes,
    check_exit_trigger,
    find_instrument_token,
    find_nearest_monthly_expiry,
    is_within_pre_expiry_buffer,
    round_to_nearest_strike,
    strangle_pnl_pct,
)


class TestRoundToNearestStrike:
    def test_rounds_down_when_closer(self):
        assert round_to_nearest_strike(1540.3, 20) == 1540

    def test_rounds_up_when_closer(self):
        assert round_to_nearest_strike(1555, 20) == 1560

    def test_exact_multiple_unchanged(self):
        assert round_to_nearest_strike(2000, 50) == 2000

    def test_rejects_zero_or_negative_step(self):
        with pytest.raises(ValueError):
            round_to_nearest_strike(1500, 0)
        with pytest.raises(ValueError):
            round_to_nearest_strike(1500, -20)

    def test_fractional_step_returns_fractional_strike(self):
        # Real NSE strikes for lower-priced stocks can be fractional (e.g.
        # WIPRO at 2.5) — must not silently truncate to a non-listed strike.
        assert round_to_nearest_strike(151.3, 2.5) == 152.5

    def test_fractional_step_whole_number_result_is_plain_int(self):
        # 155 is itself an exact multiple of 2.5 -> should come back as
        # a plain int (155), not 155.0, matching the whole-number-step case.
        result = round_to_nearest_strike(155.0, 2.5)
        assert result == 155
        assert isinstance(result, int)


class TestCalculateStrangleStrikes:
    def test_ten_percent_band_reliance_like(self):
        # spot=1300, step=20 -> raw 1430/1170, both land exactly on a
        # round()-half-to-even boundary (71.5 and 58.5) -> 1440/1160.
        call_strike, put_strike = calculate_strangle_strikes(1300.0, 0.10, 20)
        assert call_strike == 1440
        assert put_strike == 1160

    def test_call_strike_always_above_spot(self):
        call_strike, put_strike = calculate_strangle_strikes(1000.0, 0.10, 10)
        assert call_strike > 1000
        assert put_strike < 1000


class TestFindNearestMonthlyExpiry:
    def test_picks_soonest_future_date(self):
        today = date.today()
        expiries = [
            (today + timedelta(days=60)).isoformat(),
            (today + timedelta(days=5)).isoformat(),
            (today + timedelta(days=30)).isoformat(),
        ]
        nearest = find_nearest_monthly_expiry(expiries)
        assert nearest == (today + timedelta(days=5)).isoformat()

    def test_ignores_past_dates(self):
        today = date.today()
        expiries = [
            (today - timedelta(days=5)).isoformat(),
            (today + timedelta(days=10)).isoformat(),
        ]
        assert find_nearest_monthly_expiry(expiries) == (today + timedelta(days=10)).isoformat()

    def test_returns_none_if_all_past(self):
        today = date.today()
        expiries = [(today - timedelta(days=1)).isoformat()]
        assert find_nearest_monthly_expiry(expiries) is None

    def test_skips_unparseable_strings(self):
        today = date.today()
        expiries = ["not-a-date", (today + timedelta(days=3)).isoformat()]
        assert find_nearest_monthly_expiry(expiries) == (today + timedelta(days=3)).isoformat()


class TestFindInstrumentToken:
    def _chain_entry(self, strike, call_token=None, put_token=None):
        return SimpleNamespace(
            strike_price=strike,
            call_options=SimpleNamespace(instrument_key=call_token) if call_token else None,
            put_options=SimpleNamespace(instrument_key=put_token) if put_token else None,
        )

    def test_exact_strike_match(self):
        chain = [
            self._chain_entry(1400, call_token="NSE_FO|CE1400"),
            self._chain_entry(1160, put_token="NSE_FO|PE1160"),
        ]
        assert find_instrument_token(chain, 1400, "CE") == "NSE_FO|CE1400"
        assert find_instrument_token(chain, 1160, "PE") == "NSE_FO|PE1160"

    def test_falls_back_to_nearest_within_tolerance(self):
        chain = [self._chain_entry(1400, call_token="NSE_FO|CE1400")]
        # 1410 is not listed, but 1400 is within 2.5% of 1410 -> fallback applies
        assert find_instrument_token(chain, 1410, "CE") == "NSE_FO|CE1400"

    def test_returns_none_when_nothing_within_tolerance(self):
        chain = [self._chain_entry(1400, call_token="NSE_FO|CE1400")]
        # 2000 is far outside 2.5% tolerance of any listed strike
        assert find_instrument_token(chain, 2000, "CE") is None

    def test_returns_none_for_empty_chain(self):
        assert find_instrument_token([], 1400, "CE") is None

    def test_rejects_invalid_option_type(self):
        with pytest.raises(ValueError):
            find_instrument_token([], 1400, "XX")

    def test_exact_match_with_fractional_strike(self):
        # A truncating int() parse would turn 152.5 into 152 and never
        # exact-match a fractional target_strike like 152.5.
        chain = [self._chain_entry(152.5, call_token="NSE_FO|CE152_5")]
        assert find_instrument_token(chain, 152.5, "CE") == "NSE_FO|CE152_5"


class TestStranglePnlPct:
    def test_profit_when_legs_cheaper(self):
        # sold CE@10, PE@8 (premium=18); buy back at CE@3, PE@2 (cost=5)
        pct = strangle_pnl_pct(10, 8, 3, 2)
        assert pct == pytest.approx((18 - 5) / 18 * 100)

    def test_loss_when_legs_costlier(self):
        # sold CE@10, PE@8 (premium=18); buy back at CE@40, PE@2 (cost=42)
        pct = strangle_pnl_pct(10, 8, 40, 2)
        assert pct == pytest.approx((18 - 42) / 18 * 100)
        assert pct < 0

    def test_max_profit_is_100_pct_when_legs_expire_worthless(self):
        assert strangle_pnl_pct(10, 8, 0, 0) == pytest.approx(100.0)

    def test_zero_premium_returns_zero_not_a_crash(self):
        assert strangle_pnl_pct(0, 0, 5, 5) == 0.0


class TestCheckExitTrigger:
    def test_take_profit_fires_at_threshold(self):
        assert check_exit_trigger(60.0, take_profit_pct=60.0, stop_loss_pct=200.0) == "TAKE_PROFIT"

    def test_take_profit_fires_above_threshold(self):
        assert check_exit_trigger(75.0, take_profit_pct=60.0, stop_loss_pct=200.0) == "TAKE_PROFIT"

    def test_stop_loss_fires_at_threshold(self):
        assert check_exit_trigger(-200.0, take_profit_pct=60.0, stop_loss_pct=200.0) == "STOP_LOSS"

    def test_stop_loss_fires_beyond_threshold(self):
        assert check_exit_trigger(-350.0, take_profit_pct=60.0, stop_loss_pct=200.0) == "STOP_LOSS"

    def test_no_trigger_in_the_middle(self):
        assert check_exit_trigger(10.0, take_profit_pct=60.0, stop_loss_pct=200.0) is None

    def test_disabled_thresholds_never_trigger(self):
        assert check_exit_trigger(-1000.0, take_profit_pct=None, stop_loss_pct=None) is None
        assert check_exit_trigger(1000.0, take_profit_pct=None, stop_loss_pct=None) is None

    def test_stop_loss_pct_sign_is_normalized(self):
        # a negative stop_loss_pct (mistakenly passed) should behave the
        # same as its positive magnitude, not invert the check.
        assert check_exit_trigger(-200.0, take_profit_pct=None, stop_loss_pct=-200.0) == "STOP_LOSS"


class TestIsWithinPreExpiryBuffer:
    """Never wait until expiry day itself — see run_position_monitor.py's
    expiry safety net and config.py's EXIT_DAYS_BEFORE_EXPIRY."""

    def test_false_well_before_expiry(self):
        expiry = date(2026, 1, 29)
        today = date(2026, 1, 20)
        assert is_within_pre_expiry_buffer(today, expiry, exit_days_before_expiry=1) is False

    def test_false_on_the_day_still_outside_a_1_day_buffer(self):
        expiry = date(2026, 1, 29)
        today = date(2026, 1, 27)  # 2 days before expiry, buffer is 1 day
        assert is_within_pre_expiry_buffer(today, expiry, exit_days_before_expiry=1) is False

    def test_true_exactly_at_the_buffer_boundary(self):
        expiry = date(2026, 1, 29)
        today = date(2026, 1, 28)  # exactly 1 day before, buffer is 1 day
        assert is_within_pre_expiry_buffer(today, expiry, exit_days_before_expiry=1) is True

    def test_true_on_expiry_day_itself(self):
        # The buffer must never rely on someone catching it before expiry
        # day — if a check was somehow skipped until expiry day, it must
        # still fire (this is the same safety net as before, just earlier).
        expiry = date(2026, 1, 29)
        assert is_within_pre_expiry_buffer(expiry, expiry, exit_days_before_expiry=1) is True

    def test_true_past_expiry(self):
        expiry = date(2026, 1, 29)
        today = date(2026, 2, 2)
        assert is_within_pre_expiry_buffer(today, expiry, exit_days_before_expiry=1) is True

    def test_larger_buffer_fires_earlier(self):
        expiry = date(2026, 1, 29)
        today = date(2026, 1, 27)  # 2 days before expiry
        assert is_within_pre_expiry_buffer(today, expiry, exit_days_before_expiry=2) is True
        assert is_within_pre_expiry_buffer(today, expiry, exit_days_before_expiry=1) is False

    def test_zero_buffer_matches_old_expiry_day_only_behavior(self):
        expiry = date(2026, 1, 29)
        today = date(2026, 1, 28)
        assert is_within_pre_expiry_buffer(today, expiry, exit_days_before_expiry=0) is False
        assert is_within_pre_expiry_buffer(expiry, expiry, exit_days_before_expiry=0) is True
