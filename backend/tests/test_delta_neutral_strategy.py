"""
tests/test_delta_neutral_strategy.py — coverage for the pure strike-search
logic in strategies/custom/delta_neutral_strategy.py (the monthly delta-
neutral strangle engine with premium-ratio/delta-threshold adjustments).
Direct-function-call style with fakes, no real broker/DB/Black-76 chain —
same pattern as tests/test_gravity_strategy.py. find_strike_by_delta()'s
tests monkeypatch live_delta() directly so the search algorithm is tested
independently of Black-76 option-pricing math (which lives in
utils/black76.py, not this module) — the search only needs "delta falls
off monotonically moving away from ATM," not real premiums.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from compliance.sebi_rules import ComplianceError
from strategies.custom.delta_neutral_strategy import DeltaNeutralStrategy


def make_leg(instrument_key):
    return SimpleNamespace(instrument_key=instrument_key)


def make_chain_entry(strike, ce_key=None, pe_key=None):
    return SimpleNamespace(
        strike_price=strike,
        call_options=make_leg(ce_key) if ce_key else None,
        put_options=make_leg(pe_key) if pe_key else None,
    )


class FakeBroker:
    def __init__(self, strike_step=50, lot_size=75, ltp_by_token=None):
        self.strike_step = strike_step
        self.lot_size = lot_size
        self.ltp_by_token = ltp_by_token or {}
        self.dry_run = True

    def resolve_instrument_key(self, symbol):
        return f"KEY|{symbol}"

    def get_strike_step(self, symbol):
        return self.strike_step

    def get_lot_size(self, symbol):
        return self.lot_size

    def get_ltp(self, token):
        return self.ltp_by_token.get(token)


def make_strategy(rules=None, strike_step=50, lot_size=75, ltp_by_token=None) -> DeltaNeutralStrategy:
    broker = FakeBroker(strike_step=strike_step, lot_size=lot_size, ltp_by_token=ltp_by_token)
    default_rules = {
        "lots": 1, "target_delta": 0.15, "strike_grid": 100, "hedge_premium_min": 1, "hedge_premium_max": 5,
        "entry_time": "09:20", "entry_time_end": "10:00", "premium_ratio_trigger": 2.0,
        "delta_trigger_min": 0.45, "delta_trigger_max": 0.50, "reset_premium_pct": 50,
        "target_capital_pct": 5, "third_weekly_exit_time": "15:15",
    }
    if rules:
        default_rules.update(rules)
    return DeltaNeutralStrategy(
        broker=broker, audit=MagicMock(), kill_switch=MagicMock(), rate_limiter=MagicMock(),
        symbol="NIFTY", rules=default_rules, user_id=1,
    )


# ---------------------------------------------------------------------------
# _grid_strike
# ---------------------------------------------------------------------------
class TestGridStrike:
    def test_steps_by_strike_grid_not_the_raw_exchange_strike_step(self):
        strategy = make_strategy(strike_step=50, rules={"strike_grid": 100})
        assert strategy._grid_strike(24200, sign=1, step=2) == 24400   # 24200 + 2*100
        assert strategy._grid_strike(24200, sign=-1, step=1) == 24100  # 24200 - 1*100


# ---------------------------------------------------------------------------
# find_strike_by_delta — search decoupled from Black-76 via a monkeypatched live_delta
# ---------------------------------------------------------------------------
class TestFindStrikeByDelta:
    def test_finds_strike_whose_delta_is_closest_to_target(self, monkeypatch):
        chain = [make_chain_entry(s, ce_key=f"C{s}") for s in (24300, 24400, 24500, 24600)]
        strategy = make_strategy(strike_step=50, rules={"strike_grid": 100}, ltp_by_token={f"C{s}": 10 for s in (24300, 24400, 24500, 24600)})
        # Delta decays monotonically moving OTM: 24300->0.30, 24400->0.20, 24500->0.10, 24600->0.05
        deltas = {24300: 0.30, 24400: 0.20, 24500: 0.10, 24600: 0.05}
        monkeypatch.setattr(strategy, "live_delta", lambda strike, *a, **kw: deltas[strike])
        result = strategy.find_strike_by_delta(chain, "CE", atm_strike=24200, expiry="2026-02-26", futures_price=24250, target_delta=0.15)
        assert result == 24500  # |0.10-0.15|=0.05 vs |0.20-0.15|=0.05 -> tie goes to the closer-computed (candidate)

    def test_raises_when_no_strike_reaches_target_delta_within_search_cap(self, monkeypatch):
        chain = [make_chain_entry(24300, ce_key="C1")]
        strategy = make_strategy(strike_step=50, rules={"strike_grid": 100}, ltp_by_token={"C1": 10})
        monkeypatch.setattr(strategy, "live_delta", lambda *a, **kw: 0.30)  # never drops to target
        with pytest.raises(ComplianceError, match="refusing to guess"):
            strategy.find_strike_by_delta(chain, "CE", atm_strike=24200, expiry="2026-02-26", futures_price=24250, target_delta=0.15)


# ---------------------------------------------------------------------------
# find_strike_by_premium — Stage-2 reset search (real premiums, no Black-76)
# ---------------------------------------------------------------------------
class TestFindStrikeByPremium:
    def test_finds_strike_whose_premium_is_closest_to_target(self):
        chain = [make_chain_entry(s, pe_key=f"P{s}") for s in (24100, 24000, 23900)]
        ltp = {"P24100": 50, "P24000": 30, "P23900": 15}
        strategy = make_strategy(strike_step=50, rules={"strike_grid": 100}, ltp_by_token=ltp)
        result = strategy.find_strike_by_premium(chain, "PE", atm_strike=24200, target_premium=28)
        assert result == 24000  # closest to 28 among 50/30/15


# ---------------------------------------------------------------------------
# find_hedge_strike — cheapest real listed strike inside a premium band
# ---------------------------------------------------------------------------
class TestFindHedgeStrike:
    def test_finds_first_strike_in_band(self):
        chain = [make_chain_entry(s, ce_key=f"C{s}") for s in (24300, 24350, 24400)]
        ltp = {"C24300": 5, "C24350": 3, "C24400": 1.5}
        strategy = make_strategy(strike_step=50, rules={"hedge_premium_min": 1, "hedge_premium_max": 2})
        strategy.broker.ltp_by_token = ltp
        result = strategy.find_hedge_strike(chain, atm_strike=24200, option_type="CE")
        assert result == 24400

    def test_raises_when_nothing_in_band(self):
        chain = [make_chain_entry(24300, ce_key="C1")]
        strategy = make_strategy(strike_step=50, rules={"hedge_premium_min": 1, "hedge_premium_max": 2})
        strategy.broker.ltp_by_token = {"C1": 50}
        with pytest.raises(ComplianceError, match="refusing to guess"):
            strategy.find_hedge_strike(chain, atm_strike=24200, option_type="CE")


# ---------------------------------------------------------------------------
# live_delta — wiring sanity check against real Black-76 math
# ---------------------------------------------------------------------------
class TestLiveDelta:
    def test_returns_absolute_delta_for_a_deep_otm_put(self):
        strategy = make_strategy()
        # Deep OTM put (strike well below futures price) -> small |delta|, well under 0.5.
        delta = strategy.live_delta(strike=22000, option_type="PE", premium=5.0, expiry="2026-02-26", futures_price=24250)
        assert delta is not None
        assert 0 < delta < 0.3

    def test_atm_call_delta_is_near_half(self):
        strategy = make_strategy()
        delta = strategy.live_delta(strike=24250, option_type="CE", premium=200.0, expiry="2026-02-26", futures_price=24250)
        assert delta is not None
        assert 0.3 < delta < 0.7
