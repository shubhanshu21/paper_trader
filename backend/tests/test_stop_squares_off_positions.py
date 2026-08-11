"""
tests/test_stop_squares_off_positions.py — routes_custom_strategies.py::
update_strategy_status now actually squares off open legs when
transitioning to STOPPED (previously a pure status-flip that silently
abandoned any open position — see square_off_all_open_legs in
custom_strategy_scheduler.py for the underlying close logic, tested
separately in test_square_off_on_stop.py). This file covers the ROUTE-
level wiring: does it call square-off, does it require it to fully
succeed before actually flipping status, and does the DRAFT/BACKTESTING
(never-had-a-position) path stay broker-independent.

Direct-function-call style against the shared automate_test MySQL schema
(see tests/conftest.py's db_session fixture), same pattern as
test_routes_create_strategy.py.
"""
import json

import pytest
from fastapi import HTTPException

from api.routes_custom_strategies import StrategyStatusUpdate, update_strategy_status
from db.models import CustomStrategy, CustomStrategyPosition

USER = {"sub": "1"}


@pytest.fixture()
def db(db_session):
    return db_session


def _make_strategy(db, **overrides):
    defaults = {
        "user_id": 1, "name": "Live Strangle", "instrument_type": "INDEX", "strategy_type": "CUSTOM", "option_type": "BOTH",
        "symbols": json.dumps(["NIFTY"]), "status": "LIVE",
    }
    defaults.update(overrides)
    s = CustomStrategy(**defaults)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _make_leg(strategy_id, instrument_key, mode="live", status="OPEN"):
    return CustomStrategyPosition(
        strategy_id=strategy_id, leg_index=0, mode=mode, instrument_key=instrument_key,
        instrument_type="OPTION", option_type="CE", strike=24000, expiry="2026-08-27",
        transaction_type="SELL", quantity=50, entry_price=100.0, status=status,
    )


class FakeBroker:
    def __init__(self, fail=False):
        self._fail = fail
        self.orders_placed = []

    def get_ltp_batch(self, tokens):
        return dict.fromkeys(tokens, 90.0)

    def place_buy_order(self, instrument_token, quantity, order_type, tag, user_id=None, product="NRML", is_close=False):
        if self._fail:
            raise RuntimeError("broker rejected order")
        self.orders_placed.append(instrument_token)
        return "ORD1"

    def place_sell_order(self, instrument_token, quantity, order_type, tag, user_id=None, product="NRML", is_close=False):
        if self._fail:
            raise RuntimeError("broker rejected order")
        self.orders_placed.append(instrument_token)
        return "ORD1"

    def get_fill_price(self, order_id):
        return None


class TestStoppingWithNoOpenLegs:
    def test_draft_to_stopped_needs_no_broker(self, db):
        """A DRAFT strategy never entered a position — must not require a broker at all."""
        s = _make_strategy(db, status="DRAFT")
        result = update_strategy_status(s.id, StrategyStatusUpdate(status="STOPPED"), db, USER)
        assert result["status"] == "STOPPED"


class TestStoppingSquaresOffOpenLegs:
    def test_open_live_leg_is_closed_before_status_flips(self, db, monkeypatch):
        import api.custom_strategy_scheduler as sched
        s = _make_strategy(db, status="LIVE")
        leg = _make_leg(s.id, "NSE_FO|1", mode="live")
        db.add(leg)
        db.commit()

        live_broker = FakeBroker()
        monkeypatch.setattr(sched, "_get_brokers", lambda: {"live": live_broker, "paper": FakeBroker()})

        result = update_strategy_status(s.id, StrategyStatusUpdate(status="STOPPED"), db, USER)

        assert result["status"] == "STOPPED"
        db.refresh(leg)
        assert leg.status == "CLOSED"
        assert leg.exit_reason == "MANUAL_STOP"
        assert live_broker.orders_placed == ["NSE_FO|1"]

    def test_broker_unavailable_blocks_the_stop_entirely(self, db, monkeypatch):
        import api.custom_strategy_scheduler as sched
        s = _make_strategy(db, status="LIVE")
        leg = _make_leg(s.id, "NSE_FO|1", mode="live")
        db.add(leg)
        db.commit()

        monkeypatch.setattr(sched, "_get_brokers", lambda: None)

        with pytest.raises(HTTPException) as exc_info:
            update_strategy_status(s.id, StrategyStatusUpdate(status="STOPPED"), db, USER)
        assert exc_info.value.status_code == 503

        db.refresh(s)
        assert s.status == "LIVE"  # unchanged — still under active scheduler management
        db.refresh(leg)
        assert leg.status == "OPEN"

    def test_partial_square_off_failure_blocks_the_status_flip(self, db, monkeypatch):
        """
        The critical safety property: if even ONE leg fails to close, the
        strategy must NOT become STOPPED — because STOPPED strategies are
        excluded from the scheduler's main loop forever, so a leftover
        OPEN leg here would never be retried or managed again.
        """
        import api.custom_strategy_scheduler as sched
        s = _make_strategy(db, status="LIVE")
        leg = _make_leg(s.id, "NSE_FO|1", mode="live")
        db.add(leg)
        db.commit()

        failing_broker = FakeBroker(fail=True)
        monkeypatch.setattr(sched, "_get_brokers", lambda: {"live": failing_broker, "paper": FakeBroker()})
        monkeypatch.setattr(sched, "notify", lambda *a, **k: None)

        with pytest.raises(HTTPException) as exc_info:
            update_strategy_status(s.id, StrategyStatusUpdate(status="STOPPED"), db, USER)
        assert exc_info.value.status_code == 502
        assert "1" in exc_info.value.detail

        db.refresh(s)
        assert s.status == "LIVE"  # NOT stopped — leg is still open and needs management
        db.refresh(leg)
        assert leg.status == "OPEN"

    def test_a_leg_entered_while_live_still_uses_the_live_broker_after_being_paused(self, db, monkeypatch):
        """Strategy went LIVE -> PAUSED; its leg's own mode='live' must still route to the live broker, not a mode derived from current status."""
        import api.custom_strategy_scheduler as sched
        s = _make_strategy(db, status="PAUSED")
        leg = _make_leg(s.id, "NSE_FO|1", mode="live")
        db.add(leg)
        db.commit()

        live_broker = FakeBroker()
        monkeypatch.setattr(sched, "_get_brokers", lambda: {"live": live_broker, "paper": FakeBroker()})

        result = update_strategy_status(s.id, StrategyStatusUpdate(status="STOPPED"), db, USER)

        assert result["status"] == "STOPPED"
        assert live_broker.orders_placed == ["NSE_FO|1"]
