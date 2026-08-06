"""
tests/test_matrix_calendar_strategy.py — coverage for the pure delta-
search and strike-derivation logic in
strategies/custom/matrix_calendar_strategy.py (the zero-adjustment
weekly-short + weekly-hedge + monthly-calendar engine). Direct-function-
call style with fakes, no real broker/DB — same pattern as
tests/test_gravity_strategy.py.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from compliance.sebi_rules import ComplianceError
from strategies.custom.matrix_calendar_strategy import MatrixCalendarStrategy


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


def make_strategy(rules=None, strike_step=50, lot_size=75, ltp_by_token=None) -> MatrixCalendarStrategy:
    broker = FakeBroker(strike_step=strike_step, lot_size=lot_size, ltp_by_token=ltp_by_token)
    default_rules = {
        "lots": 1, "short_target_delta": 0.23, "strike_grid": 100, "weekly_hedge_points": 500,
        "weekly_expiry_offset": 1, "entry_weekday": "MON", "entry_time": "15:16", "max_hold_days": 2,
        "target_capital_pct": 1.5, "stop_loss_capital_pct": 2, "exit_days_before_expiry": 1,
    }
    if rules:
        default_rules.update(rules)
    return MatrixCalendarStrategy(
        broker=broker, audit=MagicMock(), kill_switch=MagicMock(), rate_limiter=MagicMock(),
        symbol="NIFTY", rules=default_rules, user_id=1,
    )


# ---------------------------------------------------------------------------
# _grid_strike — searches on strike_grid (100pt), lands on the real strike_step
# ---------------------------------------------------------------------------
class TestGridStrike:
    def test_steps_by_strike_grid_not_the_raw_exchange_step(self):
        strategy = make_strategy(strike_step=50, rules={"strike_grid": 100})
        assert strategy._grid_strike(24250, sign=1, step=2) == 24450  # 24250 + 2*100, rounded to 50
        assert strategy._grid_strike(24250, sign=-1, step=1) == 24150


# ---------------------------------------------------------------------------
# find_short_strike_by_delta
# ---------------------------------------------------------------------------
class TestFindShortStrikeByDelta:
    def test_finds_strike_closest_to_target_delta(self, monkeypatch):
        strikes = list(range(24250, 24850, 100))
        chain = [make_chain_entry(s, ce_key=f"C{s}") for s in strikes]
        ltp = {f"C{s}": 30 for s in strikes}
        strategy = make_strategy(strike_step=50, rules={"short_target_delta": 0.23, "strike_grid": 100}, ltp_by_token=ltp)
        deltas = {s: max(0.02, 0.45 - 0.05 * i) for i, s in enumerate(strikes)}
        monkeypatch.setattr("strategies.custom.matrix_calendar_strategy.black76.compute_greeks_from_market_price",
                             lambda **kw: {"delta": deltas[kw["K"]]})
        result = strategy.find_short_strike_by_delta(chain, "CE", atm_strike=24250, expiry="2026-08-11", futures_price=24300)
        closest = min(strikes, key=lambda s: abs(deltas[s] - 0.23))
        assert result == closest

    def test_raises_when_no_strike_reaches_target_delta(self, monkeypatch):
        chain = [make_chain_entry(24250, ce_key="C1")]
        strategy = make_strategy(strike_step=50, ltp_by_token={"C1": 30})
        monkeypatch.setattr("strategies.custom.matrix_calendar_strategy.black76.compute_greeks_from_market_price",
                             lambda **kw: {"delta": 0.45})
        with pytest.raises(ComplianceError, match="refusing to guess"):
            strategy.find_short_strike_by_delta(chain, "CE", atm_strike=24250, expiry="2026-08-11", futures_price=24300)


# ---------------------------------------------------------------------------
# compute_hedge_strikes — fixed points OTM past each sold strike
# ---------------------------------------------------------------------------
class TestComputeHedgeStrikes:
    def test_hedges_sit_outward_from_each_short_strike(self):
        strategy = make_strategy(strike_step=50, rules={"weekly_hedge_points": 500})
        hedge_ce, hedge_pe = strategy.compute_hedge_strikes(short_ce_strike=24650, short_pe_strike=23950)
        assert hedge_ce == 25150  # 24650 + 500
        assert hedge_pe == 23450  # 23950 - 500

    def test_rounds_to_the_real_strike_step(self):
        strategy = make_strategy(strike_step=50, rules={"weekly_hedge_points": 483})  # not a multiple of 50
        hedge_ce, _ = strategy.compute_hedge_strikes(short_ce_strike=24650, short_pe_strike=23950)
        assert hedge_ce % 50 == 0
