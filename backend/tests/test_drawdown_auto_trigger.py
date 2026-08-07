"""
tests/test_drawdown_auto_trigger.py — api/strategy_scheduler.py::
_check_drawdown_auto_trigger, the opt-in auto-trip of the GLOBAL kill
switch once a user's today's realized LIVE P&L breaches their configured
WalletSettings.max_daily_drawdown_pct.

get_global_kill_switch() is a real module-level singleton shared with
production code — every test here resets it in a finally block so a
failure never leaves it active for whatever test (or real process) runs
next.
"""
import json
from datetime import datetime

import pytest

from api.strategy_scheduler import _check_drawdown_auto_trigger
from compliance.sebi_rules import get_global_kill_switch
from db.models import CustomStrategy, CustomStrategyPosition, WalletSettings


@pytest.fixture()
def db(db_session):
    return db_session


@pytest.fixture(autouse=True)
def _reset_kill_switch():
    get_global_kill_switch().reset()
    yield
    get_global_kill_switch().reset()


def _make_strategy(db, user_id=1):
    s = CustomStrategy(
        user_id=user_id, name="Test Strategy", instrument_type="INDEX", strategy_type="CUSTOM",
        option_type="BOTH", symbols=json.dumps(["NIFTY"]), status="LIVE",
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _make_closed_live_leg(strategy_id, *, entry_price, exit_price, quantity=50, transaction_type="SELL", closed_today=True):
    return CustomStrategyPosition(
        strategy_id=strategy_id, leg_index=0, mode="live", instrument_key="NSE_FO|1",
        instrument_type="OPTION", option_type="CE", strike=24000, expiry="2026-08-27",
        transaction_type=transaction_type, quantity=quantity, entry_price=entry_price,
        exit_price=exit_price, status="CLOSED",
        opened_at=datetime.now(), closed_at=datetime.now() if closed_today else datetime(2020, 1, 1),
    )


class TestDrawdownAutoTrigger:
    def test_no_users_with_a_configured_threshold_is_a_noop(self, db):
        _check_drawdown_auto_trigger(db)
        assert get_global_kill_switch().is_active() is False

    def test_already_active_kill_switch_short_circuits(self, db):
        # A real config that WOULD trigger, but the switch is already on —
        # must not re-activate/re-notify.
        db.add(WalletSettings(user_id=1, starting_capital=100000, max_daily_drawdown_pct=1))
        strategy = _make_strategy(db)
        db.add(_make_closed_live_leg(strategy.id, entry_price=1000, exit_price=100, transaction_type="BUY"))  # a big BUY-side loss
        db.commit()

        get_global_kill_switch().activate(reason="already tripped")
        _check_drawdown_auto_trigger(db)
        assert get_global_kill_switch().status()["reason"] == "already tripped"  # unchanged, not overwritten

    def test_breaching_the_threshold_trips_the_global_switch(self, db, monkeypatch):
        notified = []
        monkeypatch.setattr("utils.notify.notify", lambda *a, **k: notified.append((a, k)))

        db.add(WalletSettings(user_id=1, starting_capital=100000, max_daily_drawdown_pct=2))  # 2% daily limit
        strategy = _make_strategy(db)
        # BUY entry=1000 exit=100, qty=50 -> a ~₹45,000 loss net of charges, well past 2% of ₹100,000 (₹2,000)
        db.add(_make_closed_live_leg(strategy.id, entry_price=1000, exit_price=100, transaction_type="BUY"))
        db.commit()

        _check_drawdown_auto_trigger(db)
        assert get_global_kill_switch().is_active() is True
        assert "daily drawdown limit" in get_global_kill_switch().status()["reason"]

    def test_pnl_within_threshold_does_not_trip(self, db):
        db.add(WalletSettings(user_id=1, starting_capital=100000, max_daily_drawdown_pct=50))  # generous limit
        strategy = _make_strategy(db)
        db.add(_make_closed_live_leg(strategy.id, entry_price=105, exit_price=100, transaction_type="BUY"))  # a small loss
        db.commit()

        _check_drawdown_auto_trigger(db)
        assert get_global_kill_switch().is_active() is False

    def test_paper_mode_legs_are_never_counted(self, db):
        db.add(WalletSettings(user_id=1, starting_capital=100000, max_daily_drawdown_pct=1))
        strategy = _make_strategy(db)
        leg = _make_closed_live_leg(strategy.id, entry_price=1000, exit_price=100, transaction_type="BUY")  # a big loss, if it counted
        leg.mode = "paper"  # a huge paper loss must never trip a LIVE-money safety control
        db.add(leg)
        db.commit()

        _check_drawdown_auto_trigger(db)
        assert get_global_kill_switch().is_active() is False

    def test_legs_closed_on_a_previous_day_are_not_counted_as_todays_drawdown(self, db):
        db.add(WalletSettings(user_id=1, starting_capital=100000, max_daily_drawdown_pct=1))
        strategy = _make_strategy(db)
        db.add(_make_closed_live_leg(strategy.id, entry_price=1000, exit_price=100, transaction_type="BUY", closed_today=False))
        db.commit()

        _check_drawdown_auto_trigger(db)
        assert get_global_kill_switch().is_active() is False

    def test_user_with_no_starting_capital_is_skipped_not_crashed(self, db):
        db.add(WalletSettings(user_id=1, starting_capital=0, max_daily_drawdown_pct=1))
        strategy = _make_strategy(db)
        db.add(_make_closed_live_leg(strategy.id, entry_price=1000, exit_price=100, transaction_type="BUY"))
        db.commit()

        _check_drawdown_auto_trigger(db)  # must not raise a ZeroDivisionError
        assert get_global_kill_switch().is_active() is False
