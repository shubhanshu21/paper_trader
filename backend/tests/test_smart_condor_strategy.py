"""
tests/test_smart_condor_strategy.py — coverage for the pure strike/
premium-matching logic in strategies/custom/smart_condor_strategy.py (the
weekly iron-condor engine with a live-premium-ratio adjustment protocol).
Direct-function-call style with a fake broker/chain, no real broker/DB —
same pattern as tests/test_gravity_strategy.py. Deliberately does NOT test
enter()/adjust()/_place() end-to-end (thin order-placement wrappers), only
the strike-selection math those methods delegate to.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from compliance.sebi_rules import ComplianceError
from strategies.custom.smart_condor_strategy import SmartCondorStrategy


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


def make_strategy(rules=None, strike_step=50, lot_size=75, ltp_by_token=None) -> SmartCondorStrategy:
    broker = FakeBroker(strike_step=strike_step, lot_size=lot_size, ltp_by_token=ltp_by_token)
    default_rules = {
        "lots": 1, "dte_weeks_offset": 1, "premium_round_points": 100, "hedge_points": 200,
        "entry_weekday": "MON", "entry_time": "10:15", "exit_weekday": "FRI", "exit_time": "14:30",
        "target_capital_pct": 1, "stop_loss_capital_pct": 1, "premium_ratio_trigger": 1.8, "max_adjustments_per_cycle": 2,
    }
    if rules:
        default_rules.update(rules)
    return SmartCondorStrategy(
        broker=broker, audit=MagicMock(), kill_switch=MagicMock(), rate_limiter=MagicMock(),
        symbol="NIFTY", rules=default_rules, user_id=1,
    )


# ---------------------------------------------------------------------------
# compute_initial_strikes
# ---------------------------------------------------------------------------
class TestComputeInitialStrikes:
    def test_offset_derived_from_atm_straddle_premium_rounded_to_nearest_100(self):
        # spot 24210 -> ATM 24200. CE=110, PE=95 -> straddle=205 -> round to 200.
        chain = [
            make_chain_entry(24200, ce_key="CE24200", pe_key="PE24200"),
            make_chain_entry(24400, ce_key="CE24400"),
            make_chain_entry(24000, pe_key="PE24000"),
            make_chain_entry(24600, ce_key="CE24600"),
            make_chain_entry(23800, pe_key="PE23800"),
        ]
        strategy = make_strategy(
            strike_step=50, rules={"premium_round_points": 100, "hedge_points": 200},
            ltp_by_token={"CE24200": 110, "PE24200": 95},
        )
        strikes = strategy.compute_initial_strikes(24210, chain)
        assert strikes["atm_strike"] == 24200
        assert strikes["straddle_premium"] == 205
        assert strikes["short_ce"] == 24400  # ATM + 200
        assert strikes["short_pe"] == 24000  # ATM - 200
        assert strikes["long_ce"] == 24600   # short_ce + hedge_points
        assert strikes["long_pe"] == 23800   # short_pe - hedge_points

    def test_raises_when_atm_strike_has_no_listed_contract(self):
        # find_instrument_token falls back to the nearest listed strike within
        # 2.5% of the target — use a strike far enough away (way more than
        # 2.5% of 24200) that no fallback match is possible either.
        chain = [make_chain_entry(20000, ce_key="CE20000", pe_key="PE20000")]
        strategy = make_strategy(strike_step=50)
        with pytest.raises(ComplianceError, match="no listed ATM"):
            strategy.compute_initial_strikes(24210, chain)

    def test_raises_when_atm_premium_unavailable(self):
        chain = [make_chain_entry(24200, ce_key="CE24200", pe_key="PE24200")]
        strategy = make_strategy(strike_step=50, ltp_by_token={"CE24200": 110})  # PE premium missing
        with pytest.raises(RuntimeError, match="could not fetch live"):
            strategy.compute_initial_strikes(24210, chain)


# ---------------------------------------------------------------------------
# resolve_matching_strike — search outward from ATM for a premium match
# ---------------------------------------------------------------------------
class TestResolveMatchingStrike:
    def test_finds_strike_whose_premium_is_closest_to_target_searching_upward(self):
        # CALL premiums decay moving away from ATM: 24200->150, 24250->110, 24300->80, 24350->55
        chain = [
            make_chain_entry(24200, ce_key="C0"), make_chain_entry(24250, ce_key="C1"),
            make_chain_entry(24300, ce_key="C2"), make_chain_entry(24350, ce_key="C3"),
        ]
        ltp = {"C0": 150, "C1": 110, "C2": 80, "C3": 55}
        strategy = make_strategy(strike_step=50, ltp_by_token=ltp)
        # target 82 sits between C1(110) and C2(80) — C2 is closer (|80-82|=2 vs |110-82|=28)
        result = strategy.resolve_matching_strike(chain, "CE", direction_sign=1, atm_strike=24200, target_premium=82)
        assert result == 24300

    def test_finds_strike_searching_downward_for_puts(self):
        chain = [
            make_chain_entry(24200, pe_key="P0"), make_chain_entry(24150, pe_key="P1"),
            make_chain_entry(24100, pe_key="P2"),
        ]
        ltp = {"P0": 150, "P1": 110, "P2": 80}
        strategy = make_strategy(strike_step=50, ltp_by_token=ltp)
        result = strategy.resolve_matching_strike(chain, "PE", direction_sign=-1, atm_strike=24200, target_premium=105)
        assert result == 24150  # closer to 110 than 80

    def test_raises_when_no_strike_within_search_range_matches(self):
        chain = [make_chain_entry(24200, ce_key="C0")]
        strategy = make_strategy(strike_step=50, ltp_by_token={"C0": 150})
        with pytest.raises(ComplianceError, match="refusing to guess"):
            # only one strike listed and it never drops <= target -> loop exhausts without a match
            strategy.resolve_matching_strike(chain, "CE", direction_sign=1, atm_strike=24200, target_premium=10)
