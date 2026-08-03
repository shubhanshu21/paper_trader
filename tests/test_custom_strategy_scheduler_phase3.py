"""
tests/test_custom_strategy_scheduler_phase3.py — Phase 3 coverage for
api/custom_strategy_scheduler.py: per-leg-role cycle gating (calendar
spreads), per-leg independent exit/trailing, and MA-crossover/IV-rank
conditional entry. Direct-function-call style with fakes and the shared automate_test MySQL
schema (see tests/conftest.py) — same established pattern as
tests/test_advanced_orders_scheduler.py and
tests/test_routes_backtest_runs.py. Never touches real broker/DB.
"""
import json
from unittest.mock import patch

import pytest

from automate.db.models import CustomStrategy, CustomStrategyPosition, FnoBhavcopy
import automate.api.custom_strategy_scheduler as sched


# ---------------------------------------------------------------------------
# _leg_groups
# ---------------------------------------------------------------------------
class TestLegGroups:
    def test_single_expiry_strategy_is_one_group(self):
        rules = {"legs": [{"action": "SELL"}, {"action": "SELL"}], "expiry": {"mode": "WEEKLY"}}
        assert sched._leg_groups(rules) == {"WEEKLY": [0, 1]}

    def test_calendar_spread_splits_by_leg_expiry_mode_override(self):
        rules = {
            "legs": [
                {"action": "SELL", "expiry_mode": "WEEKLY"},
                {"action": "BUY", "expiry_mode": "MONTHLY"},
            ],
            "expiry": {"mode": "WEEKLY"},
        }
        assert sched._leg_groups(rules) == {"WEEKLY": [0], "MONTHLY": [1]}

    def test_leg_without_override_falls_back_to_strategy_default(self):
        rules = {"legs": [{"action": "SELL"}, {"action": "BUY", "expiry_mode": "MONTHLY"}], "expiry": {"mode": "WEEKLY"}}
        assert sched._leg_groups(rules) == {"WEEKLY": [0], "MONTHLY": [1]}


# ---------------------------------------------------------------------------
# _get_last_entered_expiry / _set_last_entered_expiry
# ---------------------------------------------------------------------------
class TestLastEnteredExpiryRoundTrip:
    def test_no_data_yet_returns_none(self):
        strategy = CustomStrategy(last_entry_date=None)
        assert sched._get_last_entered_expiry(strategy, "NIFTY", "WEEKLY") is None

    def test_set_then_get_round_trips(self):
        strategy = CustomStrategy(last_entry_date=None)
        sched._set_last_entered_expiry(strategy, "NIFTY", "WEEKLY", "2026-01-08")
        assert sched._get_last_entered_expiry(strategy, "NIFTY", "WEEKLY") == "2026-01-08"

    def test_independent_modes_dont_clobber_each_other(self):
        strategy = CustomStrategy(last_entry_date=None)
        sched._set_last_entered_expiry(strategy, "NIFTY", "WEEKLY", "2026-01-08")
        sched._set_last_entered_expiry(strategy, "NIFTY", "MONTHLY", "2026-01-29")
        assert sched._get_last_entered_expiry(strategy, "NIFTY", "WEEKLY") == "2026-01-08"
        assert sched._get_last_entered_expiry(strategy, "NIFTY", "MONTHLY") == "2026-01-29"

    def test_old_flat_shape_degrades_gracefully_to_no_data(self):
        # Old pre-Phase-3 shape: {symbol: {"expiry": ...}} — no mode nesting.
        strategy = CustomStrategy(last_entry_date=json.dumps({"NIFTY": {"expiry": "2026-01-08"}}))
        assert sched._get_last_entered_expiry(strategy, "NIFTY", "WEEKLY") is None

    def test_independent_symbols_dont_clobber_each_other(self):
        strategy = CustomStrategy(last_entry_date=None)
        sched._set_last_entered_expiry(strategy, "NIFTY", "WEEKLY", "2026-01-08")
        sched._set_last_entered_expiry(strategy, "BANKNIFTY", "WEEKLY", "2026-01-15")
        assert sched._get_last_entered_expiry(strategy, "NIFTY", "WEEKLY") == "2026-01-08"
        assert sched._get_last_entered_expiry(strategy, "BANKNIFTY", "WEEKLY") == "2026-01-15"


# ---------------------------------------------------------------------------
# Conditional entry: MA crossover / IV rank
# ---------------------------------------------------------------------------
class FakeBroker:
    def __init__(self, ltp=None):
        self.ltp = ltp

    def resolve_instrument_key(self, symbol):
        return f"KEY|{symbol}"

    def get_ltp(self, instrument_key):
        return self.ltp


@pytest.fixture()
def bhav_session(db_session_factory):
    return db_session_factory


class TestMaCrossoverCondition:
    def test_true_when_ltp_above_the_moving_average(self, bhav_session, monkeypatch):
        session = bhav_session()
        for close in [100, 102, 104, 106, 108]:  # avg = 104
            session.add(FnoBhavcopy(symbol="NIFTY", instrument="FUTIDX", trade_date="2026-01-0" + str(close % 9 + 1),
                                     expiry_dt="2099-12-31", close=close))
        session.commit()
        session.close()
        monkeypatch.setattr(sched, "SessionLocal", bhav_session)
        broker = FakeBroker(ltp=120.0)
        condition = {"type": "MA_CROSSOVER", "period_days": 5, "direction": "ABOVE"}
        assert sched._ma_crossover_met(broker, "NIFTY", "INDEX", condition) is True

    def test_false_when_ltp_below_the_moving_average_for_above_direction(self, bhav_session, monkeypatch):
        session = bhav_session()
        for i, close in enumerate([100, 102, 104, 106, 108]):
            session.add(FnoBhavcopy(symbol="NIFTY", instrument="FUTIDX", trade_date=f"2026-01-0{i+1}",
                                     expiry_dt="2099-12-31", close=close))
        session.commit()
        session.close()
        monkeypatch.setattr(sched, "SessionLocal", bhav_session)
        broker = FakeBroker(ltp=90.0)
        condition = {"type": "MA_CROSSOVER", "period_days": 5, "direction": "ABOVE"}
        assert sched._ma_crossover_met(broker, "NIFTY", "INDEX", condition) is False

    def test_insufficient_history_never_triggers(self, bhav_session, monkeypatch):
        session = bhav_session()
        session.add(FnoBhavcopy(symbol="NIFTY", instrument="FUTIDX", trade_date="2026-01-01", expiry_dt="2099-12-31", close=100))
        session.commit()
        session.close()
        monkeypatch.setattr(sched, "SessionLocal", bhav_session)
        broker = FakeBroker(ltp=999.0)
        condition = {"type": "MA_CROSSOVER", "period_days": 20, "direction": "ABOVE"}
        assert sched._ma_crossover_met(broker, "NIFTY", "INDEX", condition) is False

    def test_malformed_condition_never_triggers(self, bhav_session, monkeypatch):
        monkeypatch.setattr(sched, "SessionLocal", bhav_session)
        broker = FakeBroker(ltp=100.0)
        assert sched._ma_crossover_met(broker, "NIFTY", "INDEX", {"type": "MA_CROSSOVER"}) is False


class TestIvRankCondition:
    def test_dispatches_to_compute_iv_rank_and_compares(self):
        with patch("automate.utils.iv_rank.compute_iv_rank", return_value=80.0):
            assert sched._iv_rank_condition_met("NIFTY", {"operator": "ABOVE", "threshold": 70.0}) is True
            assert sched._iv_rank_condition_met("NIFTY", {"operator": "BELOW", "threshold": 70.0}) is False

    def test_none_rank_never_triggers(self):
        with patch("automate.utils.iv_rank.compute_iv_rank", return_value=None):
            assert sched._iv_rank_condition_met("NIFTY", {"operator": "ABOVE", "threshold": 0.0}) is False

    def test_malformed_condition_never_triggers(self):
        assert sched._iv_rank_condition_met("NIFTY", {"operator": "SIDEWAYS", "threshold": 50.0}) is False


class TestEntryConditionMet:
    def test_dispatches_by_condition_type(self):
        with patch.object(sched, "_ma_crossover_met", return_value=True) as ma, \
             patch.object(sched, "_iv_rank_condition_met", return_value=False) as ivr:
            assert sched._entry_condition_met(FakeBroker(), "NIFTY", {"type": "MA_CROSSOVER"}, "INDEX") is True
            ma.assert_called_once()
            ivr.assert_not_called()

    def test_unknown_condition_type_never_triggers(self):
        assert sched._entry_condition_met(FakeBroker(), "NIFTY", {"type": "SOMETHING_ELSE"}, "INDEX") is False

    def test_evaluation_exception_never_propagates(self):
        with patch.object(sched, "_ma_crossover_met", side_effect=RuntimeError("boom")):
            assert sched._entry_condition_met(FakeBroker(), "NIFTY", {"type": "MA_CROSSOVER"}, "INDEX") is False


# ---------------------------------------------------------------------------
# Per-leg independent exit (TP/SL/trailing) via _try_exit_individual_leg
# ---------------------------------------------------------------------------
class FakeOrderBroker:
    def __init__(self):
        self.orders = []

    def place_buy_order(self, instrument_token, quantity, order_type, tag, user_id):
        self.orders.append(("BUY", instrument_token, quantity))
        return "ORD-1"

    def place_sell_order(self, instrument_token, quantity, order_type, tag, user_id):
        self.orders.append(("SELL", instrument_token, quantity))
        return "ORD-1"

    def get_fill_price(self, order_id):
        # Matches BaseBroker's default "unknown — fall back to LTP snapshot"
        # contract; these tests assert exit_price against now_prices anyway.
        return None


def _make_leg(**overrides):
    defaults = dict(
        id=1, strategy_id=1, leg_index=0, mode="paper", instrument_key="TOK1", instrument_type="OPTION",
        option_type="CE", strike=100, expiry="2026-01-29", transaction_type="SELL", quantity=50,
        entry_price=10.0, status="OPEN", leg_config_json=None, trail_state_json=None,
    )
    defaults.update(overrides)
    return CustomStrategyPosition(**defaults)


@pytest.fixture()
def leg_session(db_session):
    """
    _close_leg (called via _try_exit_individual_leg) atomically claims the
    leg row (OPEN -> CLOSING) via a real UPDATE before placing the broker
    order — see custom_strategy_scheduler.py::_close_leg's docstring for
    why (a race between the scheduler's own tick and a user-triggered
    Stop). That needs a real persisted row + session, not a bare
    db=None call like these tests previously used.
    """
    return db_session


def _persist_leg(db, **overrides):
    leg = _make_leg(**{k: v for k, v in overrides.items() if k != "id"})
    db.add(leg)
    db.commit()
    return leg


class TestTryExitIndividualLeg:
    def test_take_profit_closes_the_leg(self, leg_session):
        leg = _persist_leg(leg_session, leg_config_json=json.dumps({"take_profit_pct": 20.0}))
        broker = FakeOrderBroker()
        strategy = CustomStrategy(id=1, name="S", user_id=1)
        closed = sched._try_exit_individual_leg(leg_session, strategy, broker, leg, {"TOK1": 5.0})  # SELL: price fell -> big profit
        assert closed is True
        assert leg.status == "CLOSED"
        assert leg.exit_reason == "TAKE_PROFIT"
        assert broker.orders == [("BUY", "TOK1", 50)]  # opposite of SELL entry

    def test_no_trigger_leaves_leg_open_but_updates_trailing_state(self, leg_session):
        leg = _persist_leg(leg_session, leg_config_json=json.dumps(
            {"trailing": {"enabled": True, "trail_amount": 1.0, "trail_type": "points"}}
        ))
        broker = FakeOrderBroker()
        strategy = CustomStrategy(id=1, name="S", user_id=1)
        closed = sched._try_exit_individual_leg(leg_session, strategy, broker, leg, {"TOK1": 10.0})
        assert closed is False
        assert leg.status == "OPEN"
        state = json.loads(leg.trail_state_json)
        assert state["current_stop_price"] == 11.0  # SELL side: entry 10 + trail 1

    def test_trailing_stop_triggers_a_close(self, leg_session):
        leg = _persist_leg(
            leg_session,
            leg_config_json=json.dumps({"trailing": {"enabled": True, "trail_amount": 1.0, "trail_type": "points"}}),
            trail_state_json=json.dumps({"highest_price": None, "lowest_price": 10.0, "current_stop_price": 11.0}),
        )
        broker = FakeOrderBroker()
        strategy = CustomStrategy(id=1, name="S", user_id=1)
        closed = sched._try_exit_individual_leg(leg_session, strategy, broker, leg, {"TOK1": 12.0})  # crossed back above stop
        assert closed is True
        assert leg.exit_reason == "TRAILING_STOP"

    def test_missing_price_never_triggers(self, leg_session):
        leg = _persist_leg(leg_session, leg_config_json=json.dumps({"take_profit_pct": 1.0}))
        broker = FakeOrderBroker()
        strategy = CustomStrategy(id=1, name="S", user_id=1)
        closed = sched._try_exit_individual_leg(leg_session, strategy, broker, leg, {})
        assert closed is False
        assert leg.status == "OPEN"


# ---------------------------------------------------------------------------
# _try_exit end-to-end split: individually-managed vs combined-managed
# ---------------------------------------------------------------------------
@pytest.fixture()
def position_session(db_session):
    return db_session


class TestTryExitSplit:
    def test_individually_managed_leg_closes_independently_of_combined_leg(self, position_session, monkeypatch):
        db = position_session
        strategy = CustomStrategy(
            id=1, user_id=1, name="Calendar", instrument_type="INDEX", strategy_type="CUSTOM", option_type="BOTH",
            symbols=json.dumps(["NIFTY"]), status="PAPER_TRADING",
            rules_json=json.dumps({"legs": [{"action": "SELL"}, {"action": "SELL"}]}),  # no strategy-level exit config
        )
        managed_leg = CustomStrategyPosition(
            strategy_id=1, leg_index=0, mode="paper", instrument_key="TOK_MANAGED", instrument_type="OPTION",
            option_type="CE", strike=100, expiry="2026-01-29", transaction_type="SELL", quantity=50,
            entry_price=10.0, status="OPEN", leg_config_json=json.dumps({"take_profit_pct": 20.0}),
        )
        combined_leg = CustomStrategyPosition(
            strategy_id=1, leg_index=1, mode="paper", instrument_key="TOK_COMBINED", instrument_type="OPTION",
            option_type="PE", strike=100, expiry="2026-01-29", transaction_type="SELL", quantity=50,
            entry_price=8.0, status="OPEN", leg_config_json=None,
        )
        db.add_all([managed_leg, combined_leg])
        db.commit()

        monkeypatch.setattr(
            "automate.utils.instrument_cache.InstrumentCache.get_or_refresh",
            lambda self: __import__("pandas").DataFrame(columns=["instrument_key", "symbol"]),
        )
        broker = FakeOrderBroker()
        broker.get_ltp_batch = lambda tokens: {"TOK_MANAGED": 5.0, "TOK_COMBINED": 8.0}  # managed leg's own TP hit; combined flat (no strategy exit config -> never closes here)

        with patch.object(sched, "_is_leg_for_symbol", side_effect=lambda key, sym: sym == "NIFTY"):
            sched._try_exit(db, strategy, broker)

        db.refresh(managed_leg)
        db.refresh(combined_leg)
        assert managed_leg.status == "CLOSED"
        assert managed_leg.exit_reason == "TAKE_PROFIT"
        assert combined_leg.status == "OPEN"  # no strategy-level exit config and no hard stop -> untouched
