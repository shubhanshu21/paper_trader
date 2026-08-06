"""
tests/test_weekly_directional_strategy.py — coverage for the pure
direction-detection and leg-sizing logic in
strategies/custom/weekly_directional_strategy.py (the asymmetric
Reverse-Iron-Fly-with-tail-hedges engine). Direct-function-call style
with fakes, no real broker/DB — same pattern as
tests/test_gravity_strategy.py.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from compliance.sebi_rules import ComplianceError
from strategies.custom.weekly_directional_strategy import WeeklyDirectionalStrategy


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


def make_strategy(rules=None, strike_step=50, lot_size=75, ltp_by_token=None, candles=None) -> WeeklyDirectionalStrategy:
    broker = FakeBroker(strike_step=strike_step, lot_size=lot_size, ltp_by_token=ltp_by_token, candles=candles)
    default_rules = {
        "lots": 1, "ema_fast": 20, "ema_slow": 50, "expiry_offset": 0, "short_otm_points": 250,
        "tail_hedge_target_delta": 0.05, "entry_weekday": "MON", "entry_time": "09:20",
        "target_capital_pct": 10, "stop_loss_capital_pct": 5, "exit_days_before_expiry": 0,
    }
    if rules:
        default_rules.update(rules)
    return WeeklyDirectionalStrategy(
        broker=broker, audit=MagicMock(), kill_switch=MagicMock(), rate_limiter=MagicMock(),
        symbol="NIFTY", rules=default_rules, user_id=1,
    )


def candle(day: str, high: float, low: float, close: float) -> dict:
    return {"timestamp": f"{day}T00:00:00+0530", "high": high, "low": low, "close": close}


# ---------------------------------------------------------------------------
# determine_direction — EMA(fast) vs EMA(slow) current-state read
# ---------------------------------------------------------------------------
class TestDetermineDirection:
    def _ramp_candles(self, n=60, start=100.0, step=2.0, accelerate=0.15):
        # See test_macd_credit_strategy.py's identical fixture rationale — a
        # perfectly linear ramp makes two EMAs converge to numerically
        # identical values (float-noise-level differences), an unrealistic
        # edge case; mild acceleration keeps them genuinely separated.
        closes = []
        v = start
        for i in range(n):
            v += step + i * accelerate * (1 if step > 0 else -1)
            closes.append(v)
        candles = [candle(f"2026-01-{(i % 28) + 1:02d}", c + 1, c - 1, c) for i, c in enumerate(closes)]
        return candles[::-1]  # most-recent-first, matching BaseBroker's real contract

    def test_bullish_when_fast_ema_above_slow(self):
        strategy = make_strategy(candles=self._ramp_candles(step=2.0))
        assert strategy.determine_direction() == "BULLISH"

    def test_bearish_when_fast_ema_below_slow(self):
        strategy = make_strategy(candles=self._ramp_candles(step=-2.0))
        assert strategy.determine_direction() == "BEARISH"

    def test_none_with_insufficient_history_for_slow_ema(self):
        strategy = make_strategy(rules={"ema_slow": 50}, candles=self._ramp_candles(n=10))
        assert strategy.determine_direction() is None

    def test_can_pass_candles_directly_without_a_broker_call(self):
        strategy = make_strategy()
        assert strategy.determine_direction(self._ramp_candles(step=2.0)) == "BULLISH"


# ---------------------------------------------------------------------------
# compute_leg_plan — asymmetric sizing + tail-hedge side always matches the
# heavier-sold side
# ---------------------------------------------------------------------------
class TestComputeLegPlan:
    def test_bearish_sells_2x_calls_and_hedges_calls(self):
        strategy = make_strategy(strike_step=50, rules={"short_otm_points": 250})
        plan = strategy.compute_leg_plan("BEARISH", atm_strike=24250)
        assert plan["short_put_strike"] == 24000
        assert plan["short_call_strike"] == 24500
        assert plan["call_lots_multiplier"] == 2
        assert plan["put_lots_multiplier"] == 1
        assert plan["tail_hedge_option_type"] == "CE"

    def test_bullish_sells_2x_puts_and_hedges_puts(self):
        strategy = make_strategy(strike_step=50, rules={"short_otm_points": 250})
        plan = strategy.compute_leg_plan("BULLISH", atm_strike=24250)
        assert plan["put_lots_multiplier"] == 2
        assert plan["call_lots_multiplier"] == 1
        assert plan["tail_hedge_option_type"] == "PE"

    def test_strikes_round_to_the_real_strike_step(self):
        strategy = make_strategy(strike_step=50, rules={"short_otm_points": 237})  # not a multiple of 50
        plan = strategy.compute_leg_plan("BEARISH", atm_strike=24250)
        assert plan["short_put_strike"] % 50 == 0
        assert plan["short_call_strike"] % 50 == 0


# ---------------------------------------------------------------------------
# find_tail_hedge_strike — delta-targeted search outward from ATM
# ---------------------------------------------------------------------------
class TestFindTailHedgeStrike:
    def test_finds_strike_closest_to_target_delta(self, monkeypatch):
        # A complete, contiguous strike ladder — a sparse chain would let
        # find_instrument_token's nearest-strike fallback (within 2.5%)
        # silently resolve a candidate to the wrong listed strike, which
        # would only be a test-fixture artifact, not a real production gap
        # (real Upstox chains list every strike on the exchange's own grid).
        strikes = list(range(24300, 24850, 50))
        chain = [make_chain_entry(s, ce_key=f"C{s}") for s in strikes]
        ltp = {f"C{s}": 5 for s in strikes}
        strategy = make_strategy(strike_step=50, rules={"tail_hedge_target_delta": 0.05}, ltp_by_token=ltp)
        # Delta decays monotonically moving OTM from ATM (24250).
        deltas = {s: max(0.02, 0.40 - 0.04 * i) for i, s in enumerate(strikes)}
        monkeypatch.setattr("strategies.custom.weekly_directional_strategy.black76.compute_greeks_from_market_price",
                             lambda **kw: {"delta": deltas[kw["K"]]})
        result = strategy.find_tail_hedge_strike(chain, "CE", atm_strike=24250, expiry="2026-08-11", futures_price=24300)
        closest = min(strikes, key=lambda s: abs(deltas[s] - 0.05))
        assert result == closest

    def test_raises_when_no_strike_reaches_target_delta(self, monkeypatch):
        chain = [make_chain_entry(24500, ce_key="C1")]
        strategy = make_strategy(strike_step=50, ltp_by_token={"C1": 5})
        monkeypatch.setattr("strategies.custom.weekly_directional_strategy.black76.compute_greeks_from_market_price",
                             lambda **kw: {"delta": 0.30})
        with pytest.raises(ComplianceError, match="refusing to guess"):
            strategy.find_tail_hedge_strike(chain, "CE", atm_strike=24250, expiry="2026-08-11", futures_price=24300)
