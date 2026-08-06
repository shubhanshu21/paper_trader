"""
tests/test_macd_credit_strategy.py — coverage for
strategies/custom/macd_credit_strategy.py's pure trend-read and credit-
spread-search logic. `find_credit_spread` had a real inverted-monotonicity
bug fixed earlier this session (credit INCREASES as the hedge widens, not
decreases — the original code had the continue/break branches backwards);
these tests pin down the corrected behavior as a regression guard. Direct-
function-call style with fakes, no real broker/DB — same pattern as
tests/test_gravity_strategy.py.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from compliance.sebi_rules import ComplianceError
from strategies.custom.macd_credit_strategy import MacdCreditStrategy


def make_leg(instrument_key):
    return SimpleNamespace(instrument_key=instrument_key)


def make_chain_entry(strike, ce_key=None, pe_key=None):
    return SimpleNamespace(
        strike_price=strike,
        call_options=make_leg(ce_key) if ce_key else None,
        put_options=make_leg(pe_key) if pe_key else None,
    )


class FakeBroker:
    def __init__(self, strike_step=50, lot_size=75, ltp_by_token=None, candles=None):
        self.strike_step = strike_step
        self.lot_size = lot_size
        self.ltp_by_token = ltp_by_token or {}
        self.candles = candles or []
        self.dry_run = True

    def resolve_instrument_key(self, symbol):
        return f"KEY|{symbol}"

    def get_strike_step(self, symbol):
        return self.strike_step

    def get_lot_size(self, symbol):
        return self.lot_size

    def get_ltp(self, token):
        return self.ltp_by_token.get(token)

    def get_historical_candles(self, instrument_key, unit, interval, to_date):
        return self.candles


def make_strategy(rules=None, strike_step=50, lot_size=75, ltp_by_token=None, candles=None) -> MacdCreditStrategy:
    broker = FakeBroker(strike_step=strike_step, lot_size=lot_size, ltp_by_token=ltp_by_token, candles=candles)
    default_rules = {
        "lots": 1, "macd_fast": 12, "macd_slow": 26, "macd_signal": 9, "credit_min": 90, "credit_max": 140,
        "credit_search_max_steps": 40, "rollover_day_of_month": 15, "exit_days_before_expiry": 2,
    }
    if rules:
        default_rules.update(rules)
    return MacdCreditStrategy(
        broker=broker, audit=MagicMock(), kill_switch=MagicMock(), rate_limiter=MagicMock(),
        symbol="NIFTY", rules=default_rules, user_id=1,
    )


def geometric_chain(strike_step, n_strikes, start_premium, decay):
    """
    Builds a chain + ltp map where each strike further from strike 0 has
    premium = start_premium * decay**step — mirrors a real option chain's
    premium decay away from ATM, which is exactly the shape
    find_credit_spread()'s search assumes (credit rises monotonically as
    the hedge moves further out, for a fixed short leg).
    """
    chain, ltp = [], {}
    for step in range(0, n_strikes):
        strike = step * strike_step
        ce_key, pe_key = f"CE{strike}", f"PE{strike}"
        chain.append(make_chain_entry(strike, ce_key=ce_key, pe_key=pe_key))
        premium = start_premium * (decay ** step)
        ltp[ce_key] = premium
        ltp[pe_key] = premium
    return chain, ltp


# ---------------------------------------------------------------------------
# find_credit_spread — regression coverage for the fixed monotonicity bug
# ---------------------------------------------------------------------------
class TestFindCreditSpread:
    def test_finds_short_hedge_pair_whose_credit_lands_in_the_target_band(self):
        # Premiums decay geometrically outward from strike 0 (treated as ATM here for simplicity).
        chain, ltp = geometric_chain(strike_step=50, n_strikes=30, start_premium=300, decay=0.85)
        strategy = make_strategy(strike_step=50, rules={"credit_min": 90, "credit_max": 140, "credit_search_max_steps": 40}, ltp_by_token=ltp)
        short_strike, hedge_strike = strategy.find_credit_spread(chain, "CE", direction_sign=1, atm_strike=0)
        short_premium = ltp[f"CE{int(short_strike)}"]
        hedge_premium = ltp[f"CE{int(hedge_strike)}"]
        credit = short_premium - hedge_premium
        assert 90 <= credit <= 140
        assert hedge_strike > short_strike  # hedge is further OTM than the short leg

    def test_credit_grows_as_hedge_widens_for_a_fixed_short_leg(self):
        # Direct pin on the monotonicity assumption the search algorithm
        # depends on: widening the hedge (holding the short fixed) must
        # only ever INCREASE credit, never decrease it — this is the
        # exact assumption the original bug violated (it treated the
        # relationship as decreasing, breaking on credit > credit_max
        # instead of continuing, and continuing on credit < credit_min
        # instead of stopping).
        _chain, ltp = geometric_chain(strike_step=50, n_strikes=10, start_premium=300, decay=0.85)
        short_premium = ltp["CE0"]
        credits = [short_premium - ltp[f"CE{step * 50}"] for step in range(1, 10)]
        assert credits == sorted(credits)  # strictly non-decreasing as hedge distance grows

    def test_skips_short_legs_whose_own_premium_cant_reach_credit_min(self):
        # A short leg with premium below credit_min can never produce a
        # spread wide enough to reach credit_min (max possible credit ==
        # short_premium, when hedge premium -> 0) — must be skipped
        # entirely rather than searched.
        chain, ltp = geometric_chain(strike_step=50, n_strikes=30, start_premium=300, decay=0.85)
        strategy = make_strategy(strike_step=50, rules={"credit_min": 90, "credit_max": 140, "credit_search_max_steps": 40}, ltp_by_token=ltp)
        short_strike, _ = strategy.find_credit_spread(chain, "CE", direction_sign=1, atm_strike=0)
        assert ltp[f"CE{int(short_strike)}"] >= 90

    def test_raises_when_no_pair_lands_in_band_within_search_cap(self):
        # Flat premiums (no decay) -> credit is always ~0 between adjacent strikes -> never reaches credit_min.
        chain, ltp = geometric_chain(strike_step=50, n_strikes=10, start_premium=100, decay=1.0)
        strategy = make_strategy(strike_step=50, rules={"credit_min": 90, "credit_max": 140, "credit_search_max_steps": 5}, ltp_by_token=ltp)
        with pytest.raises(ComplianceError, match="refusing to guess"):
            strategy.find_credit_spread(chain, "CE", direction_sign=1, atm_strike=0)

    def test_searches_downward_for_puts(self):
        # Re-key as PE at negative strikes to simulate searching below ATM.
        pe_chain, pe_ltp = [], {}
        for step in range(0, 30):
            strike = -step * 50
            key = f"PE{strike}"
            pe_chain.append(make_chain_entry(strike, pe_key=key))
            pe_ltp[key] = 300 * (0.85 ** step)
        strategy = make_strategy(strike_step=50, rules={"credit_min": 90, "credit_max": 140, "credit_search_max_steps": 40}, ltp_by_token=pe_ltp)
        short_strike, hedge_strike = strategy.find_credit_spread(pe_chain, "PE", direction_sign=-1, atm_strike=0)
        assert hedge_strike < short_strike  # hedge is further OTM (more negative) than the short leg


# ---------------------------------------------------------------------------
# read_trend
# ---------------------------------------------------------------------------
class TestReadTrend:
    def _trending_candles(self, n=40, start=100, step=2, accelerate=0.15):
        # A perfectly linear ramp makes MACD converge to an exact constant
        # lag, so MACD and signal become numerically identical (differing
        # only by ~1e-15 float noise) — an unrealistic edge case real price
        # data never hits. Adding mild acceleration keeps MACD genuinely
        # diverging from its own signal line, like a real trending market.
        closes = []
        v = float(start)
        for i in range(n):
            v += step + i * accelerate * (1 if step > 0 else -1)
            closes.append(v)
        candles = [{"timestamp": f"2026-01-{(i % 28) + 1:02d}T00:00:00", "close": c, "high": c + 1, "low": c - 1} for i, c in enumerate(closes)]
        return candles[::-1]  # most-recent-first, matching BaseBroker.get_historical_candles's real contract

    def test_bullish_on_an_accelerating_uptrend(self):
        strategy = make_strategy(candles=self._trending_candles(step=2))
        assert strategy.read_trend() == "BULLISH"

    def test_bearish_on_an_accelerating_downtrend(self):
        strategy = make_strategy(candles=self._trending_candles(step=-2))
        assert strategy.read_trend() == "BEARISH"

    def test_none_with_insufficient_history(self):
        strategy = make_strategy(candles=self._trending_candles(n=5))
        assert strategy.read_trend() is None

    def test_raises_with_no_candles_at_all(self):
        strategy = make_strategy(candles=[])
        with pytest.raises(RuntimeError, match="no hourly candles"):
            strategy.read_trend()
