"""
tests/test_advanced_orders_scheduler.py — Tests for the tick functions in
advanced_orders_scheduler.py: OCO settlement, trailing-stop advancement/
exit, and bracket entry->TP/SL arming, across both paper and live mode.

No network, no DB — db.models.AdvancedOrder objects are built in memory
(never added to a session) since the tick functions only read/mutate
.status/.state_json and don't touch the ORM relationship machinery.
FakeBroker stands in for PaperBroker/UpstoxBroker.
"""
import json
from unittest.mock import patch

import pytest

from automate.api.advanced_orders_scheduler import (
    _tick_bracket, _tick_oco, _tick_trailing_stop,
)
from automate.db.models import AdvancedOrder


class FakeBroker:
    def __init__(self, ltp=None, order_status=None):
        self.ltp = ltp or {}
        self.order_status = order_status or {}
        self.placed = []
        self.cancelled = []
        self.modified = []

    def get_ltp(self, instrument_token):
        return self.ltp.get(instrument_token)

    def get_order_status(self, order_id):
        return self.order_status.get(order_id)

    def place_sell_order(self, instrument_token, quantity, product="D", order_type="MARKET",
                          tag="", user_id=None, price=0, trigger_price=0):
        oid = f"SELL-{len(self.placed) + 1}"
        self.placed.append(dict(order_id=oid, side="SELL", instrument_token=instrument_token,
                                 order_type=order_type, price=price, trigger_price=trigger_price))
        return oid

    def place_buy_order(self, instrument_token, quantity, product="D", order_type="MARKET",
                         tag="", user_id=None, price=0, trigger_price=0):
        oid = f"BUY-{len(self.placed) + 1}"
        self.placed.append(dict(order_id=oid, side="BUY", instrument_token=instrument_token,
                                 order_type=order_type, price=price, trigger_price=trigger_price))
        return oid

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return True

    def modify_order(self, order_id, order_type, price=0, trigger_price=0, quantity=None):
        # Mirrors the real UpstoxBroker signature — order_type is
        # positional/required, so a caller that omits it (the exact bug
        # this test suite exists to catch) raises TypeError here.
        self.modified.append(dict(order_id=order_id, order_type=order_type,
                                   price=price, trigger_price=trigger_price, quantity=quantity))
        return True


def _order(kind, mode, state, user_id=1):
    o = AdvancedOrder(public_id=f"{kind.lower()}_test", kind=kind, user_id=user_id, mode=mode,
                       status="ACTIVE", state_json=json.dumps(state))
    return o


class TestTickOcoPaper:
    def test_primary_trigger_completes_order_and_cancels_secondary(self):
        broker = FakeBroker(ltp={"CE": 105})
        state = {
            "primary_order": {"status": "PENDING", "instrument_token": "CE", "transaction_type": "SELL",
                               "order_type": "LIMIT", "price": 100, "quantity": 1},
            "secondary_order": {"status": "PENDING", "instrument_token": "PE", "transaction_type": "BUY",
                                 "order_type": "LIMIT", "price": 50, "quantity": 1},
        }
        order = _order("OCO", "paper", state)
        _tick_oco(order, {"paper": broker})

        state = json.loads(order.state_json)
        assert order.status == "COMPLETED"
        assert state["primary_order"]["status"] == "COMPLETE"
        assert state["secondary_order"]["status"] == "CANCELLED"
        assert len(broker.placed) == 1  # only the triggered leg was filled

    def test_neither_leg_triggered_stays_active(self):
        broker = FakeBroker(ltp={"CE": 90, "PE": 90})
        state = {
            "primary_order": {"status": "PENDING", "instrument_token": "CE", "transaction_type": "SELL",
                               "order_type": "LIMIT", "price": 100, "quantity": 1},
            "secondary_order": {"status": "PENDING", "instrument_token": "PE", "transaction_type": "BUY",
                                 "order_type": "LIMIT", "price": 50, "quantity": 1},
        }
        order = _order("OCO", "paper", state)
        _tick_oco(order, {"paper": broker})

        assert order.status == "ACTIVE"
        assert len(broker.placed) == 0

    def test_failed_paper_fill_does_not_complete_order(self):
        broker = FakeBroker(ltp={"CE": 105})

        def failing_place_sell_order(*a, **k):
            raise RuntimeError("simulated wallet rejection")
        broker.place_sell_order = failing_place_sell_order

        state = {
            "primary_order": {"status": "PENDING", "instrument_token": "CE", "transaction_type": "SELL",
                               "order_type": "LIMIT", "price": 100, "quantity": 1},
            "secondary_order": {"status": "PENDING", "instrument_token": "PE", "transaction_type": "BUY",
                                 "order_type": "LIMIT", "price": 999999, "quantity": 1},
        }
        order = _order("OCO", "paper", state)
        _tick_oco(order, {"paper": broker})

        assert order.status == "ACTIVE"
        state = json.loads(order.state_json)
        assert state["primary_order"]["status"] == "PENDING"


class TestTickOcoLive:
    def test_primary_fill_cancels_secondary(self):
        broker = FakeBroker(order_status={"P1": "complete"})
        state = {
            "primary_order": {"status": "PLACED", "order_id": "P1"},
            "secondary_order": {"status": "PLACED", "order_id": "S1"},
        }
        order = _order("OCO", "live", state)
        _tick_oco(order, {"live": broker})

        assert order.status == "COMPLETED"
        assert broker.cancelled == ["S1"]
        state = json.loads(order.state_json)
        assert state["secondary_order"]["status"] == "CANCELLED"

    def test_both_rejected_marks_completed_settled(self):
        broker = FakeBroker(order_status={"P1": "rejected", "S1": "rejected"})
        state = {
            "primary_order": {"status": "PLACED", "order_id": "P1"},
            "secondary_order": {"status": "PLACED", "order_id": "S1"},
        }
        order = _order("OCO", "live", state)
        _tick_oco(order, {"live": broker})

        assert order.status == "COMPLETED"
        assert broker.cancelled == []  # nothing left to cancel — both already terminal


class TestTickTrailingStopPaper:
    def test_long_position_stop_advances_upward_with_price(self):
        broker = FakeBroker(ltp={"X": 110})
        state = {
            "instrument_token": "X", "symbol": "X", "side": "BUY", "quantity": 10,
            "trail_amount": 5, "trail_type": "points", "product": "D",
            "current_stop_price": None, "highest_price": None, "lowest_price": None,
            "broker_order_id": None, "exit_order_id": None, "exit_price": None,
        }
        order = _order("TRAILING_STOP", "paper", state)
        _tick_trailing_stop(order, {"paper": broker})

        state = json.loads(order.state_json)
        assert state["highest_price"] == 110
        assert state["current_stop_price"] == 105
        assert order.status == "ACTIVE"

    def test_long_position_exits_when_price_falls_to_stop(self):
        broker = FakeBroker(ltp={"X": 110})
        state = {
            "instrument_token": "X", "symbol": "X", "side": "BUY", "quantity": 10,
            "trail_amount": 5, "trail_type": "points", "product": "D",
            "current_stop_price": None, "highest_price": None, "lowest_price": None,
            "broker_order_id": None, "exit_order_id": None, "exit_price": None,
        }
        order = _order("TRAILING_STOP", "paper", state)
        _tick_trailing_stop(order, {"paper": broker})  # anchors at 110, stop=105

        broker.ltp["X"] = 104  # price falls through the stop
        _tick_trailing_stop(order, {"paper": broker})

        assert order.status == "COMPLETED"
        state = json.loads(order.state_json)
        assert state["exit_price"] == 104
        assert state["exit_order_id"] is not None
        assert broker.placed[0]["side"] == "SELL"

    def test_short_position_stop_advances_downward_with_price(self):
        broker = FakeBroker(ltp={"X": 90})
        state = {
            "instrument_token": "X", "symbol": "X", "side": "SELL", "quantity": 10,
            "trail_amount": 5, "trail_type": "points", "product": "D",
            "current_stop_price": None, "highest_price": None, "lowest_price": None,
            "broker_order_id": None, "exit_order_id": None, "exit_price": None,
        }
        order = _order("TRAILING_STOP", "paper", state)
        _tick_trailing_stop(order, {"paper": broker})

        state = json.loads(order.state_json)
        assert state["lowest_price"] == 90
        assert state["current_stop_price"] == 95

    def test_failed_paper_exit_fill_stays_active(self):
        broker = FakeBroker(ltp={"X": 110})
        state = {
            "instrument_token": "X", "symbol": "X", "side": "BUY", "quantity": 10,
            "trail_amount": 5, "trail_type": "points", "product": "D",
            "current_stop_price": None, "highest_price": None, "lowest_price": None,
            "broker_order_id": None, "exit_order_id": None, "exit_price": None,
        }
        order = _order("TRAILING_STOP", "paper", state)
        _tick_trailing_stop(order, {"paper": broker})  # anchors, stop=105

        def failing_place_sell_order(*a, **k):
            raise RuntimeError("simulated wallet rejection")
        broker.place_sell_order = failing_place_sell_order
        broker.ltp["X"] = 100  # crosses the stop

        _tick_trailing_stop(order, {"paper": broker})

        # Must NOT be marked COMPLETED off a failed fill (this was a real
        # bug: the order used to be marked COMPLETED with exit_order_id=None).
        assert order.status == "ACTIVE"
        state = json.loads(order.state_json)
        assert state["exit_order_id"] is None


class TestTickTrailingStopLive:
    def test_places_initial_stop_order_on_first_tick(self):
        broker = FakeBroker(ltp={"X": 110})
        state = {
            "instrument_token": "X", "symbol": "X", "side": "BUY", "quantity": 10,
            "trail_amount": 5, "trail_type": "points", "product": "D",
            "current_stop_price": None, "highest_price": None, "lowest_price": None,
            "broker_order_id": None, "exit_order_id": None, "exit_price": None,
        }
        order = _order("TRAILING_STOP", "live", state)
        _tick_trailing_stop(order, {"live": broker})

        state = json.loads(order.state_json)
        assert state["broker_order_id"] is not None
        assert broker.placed[0]["order_type"] == "SL-M"
        assert broker.placed[0]["trigger_price"] == 105

    def test_advances_trigger_via_modify_order_with_required_fields(self):
        """
        Regression test: ModifyOrderRequest.order_type/price/trigger_price
        are all mandatory in the Upstox SDK (raise ValueError if None) —
        modify_order() must be called with order_type set, not just
        trigger_price, or every live trailing-stop advance silently fails.
        """
        broker = FakeBroker(ltp={"X": 110})
        state = {
            "instrument_token": "X", "symbol": "X", "side": "BUY", "quantity": 10,
            "trail_amount": 5, "trail_type": "points", "product": "D",
            "current_stop_price": 105, "highest_price": 110, "lowest_price": None,
            "broker_order_id": "SLM-1", "exit_order_id": None, "exit_price": None,
        }
        order = _order("TRAILING_STOP", "live", state)
        broker.order_status["SLM-1"] = "open"

        broker.ltp["X"] = 120  # price improved — stop should advance
        _tick_trailing_stop(order, {"live": broker})

        assert len(broker.modified) == 1
        call = broker.modified[0]
        assert call["order_id"] == "SLM-1"
        assert call["order_type"] == "SL-M"  # the field that was missing before the fix
        assert call["trigger_price"] == 115

    def test_fill_marks_completed(self):
        broker = FakeBroker(ltp={"X": 104}, order_status={"SLM-1": "complete"})
        state = {
            "instrument_token": "X", "symbol": "X", "side": "BUY", "quantity": 10,
            "trail_amount": 5, "trail_type": "points", "product": "D",
            "current_stop_price": 105, "highest_price": 110, "lowest_price": None,
            "broker_order_id": "SLM-1", "exit_order_id": None, "exit_price": None,
        }
        order = _order("TRAILING_STOP", "live", state)
        _tick_trailing_stop(order, {"live": broker})

        assert order.status == "COMPLETED"
        state = json.loads(order.state_json)
        assert state["exit_order_id"] == "SLM-1"

    def test_repeated_modify_failure_sends_notify_alert(self):
        broker = FakeBroker(ltp={"X": 110})
        broker.modify_order = lambda *a, **k: False  # simulate broker rejecting the modify
        state = {
            "instrument_token": "X", "symbol": "X", "side": "BUY", "quantity": 10,
            "trail_amount": 5, "trail_type": "points", "product": "D",
            "current_stop_price": 100, "highest_price": 105, "lowest_price": None,
            "broker_order_id": "SLM-1", "exit_order_id": None, "exit_price": None,
        }
        order = _order("TRAILING_STOP", "live", state)
        broker.order_status["SLM-1"] = "open"

        with patch("automate.api.advanced_orders_scheduler.notify") as mock_notify:
            for ltp in (111, 112, 113):
                broker.ltp["X"] = ltp
                _tick_trailing_stop(order, {"live": broker})
            assert mock_notify.call_count == 1  # only once the 3rd consecutive failure hits


class TestTickBracket:
    def test_paper_entry_already_complete_arms_tp_sl_pair(self):
        broker = FakeBroker(ltp={"X": 100})
        state = {
            "entry_order": {"status": "COMPLETE", "order_id": "E1"},
            "take_profit": {"status": "PENDING", "instrument_token": "X", "transaction_type": "SELL",
                             "order_type": "LIMIT", "price": 110, "quantity": 1},
            "stop_loss": {"status": "PENDING", "instrument_token": "X", "transaction_type": "SELL",
                          "order_type": "SL-M", "trigger_price": 90, "quantity": 1},
        }
        order = _order("BRACKET", "paper", state)

        broker.ltp["X"] = 111  # take-profit triggers
        _tick_bracket(order, {"paper": broker})

        assert order.status == "COMPLETED"
        state = json.loads(order.state_json)
        assert state["take_profit"]["status"] == "COMPLETE"
        assert state["stop_loss"]["status"] == "CANCELLED"

    def test_live_entry_still_placed_does_not_arm_tp_sl_yet(self):
        broker = FakeBroker(order_status={"E1": "open"})
        state = {
            "entry_order": {"status": "PLACED", "order_id": "E1"},
            "take_profit": {"status": "PENDING", "instrument_token": "X", "transaction_type": "SELL",
                             "order_type": "LIMIT", "price": 110, "quantity": 1},
            "stop_loss": {"status": "PENDING", "instrument_token": "X", "transaction_type": "SELL",
                          "order_type": "SL-M", "trigger_price": 90, "quantity": 1},
        }
        order = _order("BRACKET", "live", state)
        _tick_bracket(order, {"live": broker})

        assert order.status == "ACTIVE"
        assert len(broker.placed) == 0

    def test_live_entry_fills_then_arms_tp_sl_on_next_tick(self):
        broker = FakeBroker(order_status={"E1": "open"})
        state = {
            "entry_order": {"status": "PLACED", "order_id": "E1"},
            "take_profit": {"status": "PENDING", "instrument_token": "X", "transaction_type": "SELL",
                             "order_type": "LIMIT", "price": 110, "quantity": 1},
            "stop_loss": {"status": "PENDING", "instrument_token": "X", "transaction_type": "SELL",
                          "order_type": "SL-M", "trigger_price": 90, "quantity": 1},
        }
        order = _order("BRACKET", "live", state)

        broker.order_status["E1"] = "complete"
        _tick_bracket(order, {"live": broker})

        state = json.loads(order.state_json)
        assert state["entry_order"]["status"] == "COMPLETE"
        assert state["take_profit"]["status"] == "PLACED"
        assert state["stop_loss"]["status"] == "PLACED"
        assert order.status == "ACTIVE"  # TP/SL now resting, not yet settled

    def test_live_entry_rejected_cancels_bracket(self):
        broker = FakeBroker(order_status={"E1": "rejected"})
        state = {
            "entry_order": {"status": "PLACED", "order_id": "E1"},
            "take_profit": {"status": "PENDING"},
            "stop_loss": {"status": "PENDING"},
        }
        order = _order("BRACKET", "live", state)
        _tick_bracket(order, {"live": broker})

        assert order.status == "CANCELLED"
