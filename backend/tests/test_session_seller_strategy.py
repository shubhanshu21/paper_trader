"""
tests/test_session_seller_strategy.py — coverage for the pure hedge-strike-
search logic in strategies/custom/session_seller_strategy.py (the dual-
session NIFTY/SENSEX intraday selling engine) plus the blackout-date guard
in api/session_seller_engine.py. Direct-function-call style with fakes, no
real broker/DB — same pattern as tests/test_gravity_strategy.py.
"""
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from api.session_seller_engine import _is_blacked_out
from compliance.sebi_rules import ComplianceError, KillSwitch
from strategies.custom.session_seller_strategy import SessionSellerStrategy


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


def make_strategy(rules=None, strike_step=50, lot_size=75, ltp_by_token=None) -> SessionSellerStrategy:
    broker = FakeBroker(strike_step=strike_step, lot_size=lot_size, ltp_by_token=ltp_by_token)
    default_rules = {
        "lots": 1,
        "symbol_schedule": {"MON": "NIFTY", "TUE": "NIFTY", "WED": "SENSEX", "THU": "SENSEX", "FRI": "NIFTY"},
        "sessions": {
            "NIFTY": {"morning_entry": "09:20", "morning_exit": "11:30", "afternoon_entry": "12:30", "afternoon_exit": "15:15"},
            "SENSEX": {"morning_entry": "09:20", "morning_exit": "11:30", "afternoon_entry": "12:30", "afternoon_exit": "15:15"},
        },
        "otm_points": 100, "hedge_premium_min": 1, "hedge_premium_max": 2, "stop_loss_pct": 50, "blackout_dates": [],
    }
    if rules:
        default_rules.update(rules)
    return SessionSellerStrategy(
        broker=broker, audit=MagicMock(), kill_switch=MagicMock(), rate_limiter=MagicMock(),
        symbol="NIFTY", rules=default_rules, user_id=1,
    )


# ---------------------------------------------------------------------------
# find_hedge_strike — nearest OTM strike whose live premium sits in the band
# ---------------------------------------------------------------------------
class TestFindHedgeStrike:
    def test_finds_first_call_strike_with_premium_inside_the_band(self):
        # Premiums decay moving OTM: 24300->5, 24350->3, 24400->1.5, 24450->0.8
        chain = [
            make_chain_entry(24300, ce_key="C1"), make_chain_entry(24350, ce_key="C2"),
            make_chain_entry(24400, ce_key="C3"), make_chain_entry(24450, ce_key="C4"),
        ]
        ltp = {"C1": 5, "C2": 3, "C3": 1.5, "C4": 0.8}
        strategy = make_strategy(strike_step=50, rules={"hedge_premium_min": 1, "hedge_premium_max": 2})
        strategy.broker.ltp_by_token = ltp
        result = strategy.find_hedge_strike(chain, atm_strike=24200, option_type="CE")
        assert result == 24400  # first strike (searching outward) whose premium (1.5) is in [1,2]

    def test_searches_downward_for_puts(self):
        chain = [make_chain_entry(24100, pe_key="P1"), make_chain_entry(24050, pe_key="P2")]
        strategy = make_strategy(strike_step=50, rules={"hedge_premium_min": 1, "hedge_premium_max": 2})
        strategy.broker.ltp_by_token = {"P1": 5, "P2": 1.5}
        result = strategy.find_hedge_strike(chain, atm_strike=24200, option_type="PE")
        assert result == 24050

    def test_raises_when_nothing_in_band_within_search_range(self):
        chain = [make_chain_entry(24300, ce_key="C1")]
        strategy = make_strategy(strike_step=50, rules={"hedge_premium_min": 1, "hedge_premium_max": 2})
        strategy.broker.ltp_by_token = {"C1": 50}  # never drops into [1,2] within the one listed strike
        with pytest.raises(ComplianceError, match="refusing to guess"):
            strategy.find_hedge_strike(chain, atm_strike=24200, option_type="CE")


# ---------------------------------------------------------------------------
# _is_blacked_out (api/session_seller_engine.py)
# ---------------------------------------------------------------------------
class TestIsBlackedOut:
    def test_true_inside_a_window(self):
        rules = {"blackout_dates": [{"start": "2026-03-01", "end": "2026-03-05"}]}
        assert _is_blacked_out(rules, date(2026, 3, 3)) is True

    def test_false_outside_any_window(self):
        rules = {"blackout_dates": [{"start": "2026-03-01", "end": "2026-03-05"}]}
        assert _is_blacked_out(rules, date(2026, 3, 10)) is False

    def test_false_with_no_windows(self):
        assert _is_blacked_out({"blackout_dates": []}, date(2026, 3, 3)) is False


# ---------------------------------------------------------------------------
# Kill switch enforcement — entries blocked, closes always allowed
# ---------------------------------------------------------------------------
class OrderPlacingFakeBroker(FakeBroker):
    """FakeBroker plus the order-placement surface _place() actually calls."""
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.placed_orders = []

    def place_sell_order(self, instrument_token, quantity, product, order_type, tag, user_id=None):
        self.placed_orders.append(("SELL", instrument_token))
        return "ORDER-1"

    def place_buy_order(self, instrument_token, quantity, product, order_type, tag, user_id=None):
        self.placed_orders.append(("BUY", instrument_token))
        return "ORDER-1"

    def get_fill_price(self, order_id):
        return 1.5


def make_strategy_with_kill_switch(kill_switch: KillSwitch) -> SessionSellerStrategy:
    broker = OrderPlacingFakeBroker(strike_step=50, lot_size=75, ltp_by_token={"C1": 1.5})
    rules = {
        "lots": 1,
        "symbol_schedule": {"MON": "NIFTY"},
        "sessions": {"NIFTY": {"morning_entry": "09:20", "morning_exit": "11:30", "afternoon_entry": "12:30", "afternoon_exit": "15:15"}},
        "otm_points": 100, "hedge_premium_min": 1, "hedge_premium_max": 2, "stop_loss_pct": 50, "blackout_dates": [],
    }
    return SessionSellerStrategy(
        broker=broker, audit=MagicMock(), kill_switch=kill_switch, rate_limiter=MagicMock(),
        symbol="NIFTY", rules=rules, user_id=1,
    )


class TestKillSwitchEnforcement:
    def _chain(self):
        return [make_chain_entry(24000, ce_key="C1")]

    def test_active_kill_switch_blocks_a_new_short(self):
        ks = KillSwitch()
        ks.activate(reason="test")
        strategy = make_strategy_with_kill_switch(ks)
        with pytest.raises(RuntimeError, match="KILL SWITCH IS ACTIVE"):
            strategy.sell_short(chain=self._chain(), expiry="2026-08-27", option_type="CE", strike=24000)
        assert strategy.broker.placed_orders == []  # never reached the broker at all

    def test_active_kill_switch_blocks_a_new_hedge(self):
        ks = KillSwitch()
        ks.activate(reason="test")
        strategy = make_strategy_with_kill_switch(ks)
        with pytest.raises(RuntimeError, match="KILL SWITCH IS ACTIVE"):
            strategy.buy_hedge(chain=self._chain(), expiry="2026-08-27", option_type="CE", strike=24000)

    def test_active_kill_switch_does_not_block_closing_an_existing_leg(self):
        # The whole point of NOT guarding close_leg: halting new order flow
        # must never also trap an already-open real position with no way
        # to exit while the switch is active.
        ks = KillSwitch()
        ks.activate(reason="test")
        strategy = make_strategy_with_kill_switch(ks)
        result = strategy.close_leg(instrument_token="C1", quantity=75, strike=24000, option_type="CE", original_transaction_type="SELL")
        assert result["order_id"] == "ORDER-1"
        assert strategy.broker.placed_orders == [("BUY", "C1")]

    def test_inactive_kill_switch_allows_a_new_short(self):
        strategy = make_strategy_with_kill_switch(KillSwitch())
        result = strategy.sell_short(chain=self._chain(), expiry="2026-08-27", option_type="CE", strike=24000)
        assert result["order_id"] == "ORDER-1"
