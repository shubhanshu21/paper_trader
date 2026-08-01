"""
tests/test_advanced_orders_common.py — Tests for advanced_orders_common.py's
leg placement/trigger/validation helpers, shared by routes_advanced_orders.py
and advanced_orders_scheduler.py. No network, no DB — a FakeBroker stands in
for PaperBroker/UpstoxBroker.
"""
from automate.api.advanced_orders_common import (
    leg_triggered, place_leg, simulate_paper_fill, validate_leg,
)


class FakeBroker:
    def __init__(self, ltp=None):
        self.ltp = ltp if ltp is not None else {}
        self.placed = []

    def get_ltp(self, instrument_token):
        return self.ltp.get(instrument_token)

    def place_sell_order(self, instrument_token, quantity, product="D", order_type="MARKET",
                          tag="", user_id=None, price=0, trigger_price=0):
        self.placed.append(dict(side="SELL", instrument_token=instrument_token, quantity=quantity,
                                 order_type=order_type, tag=tag, price=price, trigger_price=trigger_price))
        return f"SELL-{len(self.placed)}"

    def place_buy_order(self, instrument_token, quantity, product="D", order_type="MARKET",
                         tag="", user_id=None, price=0, trigger_price=0):
        self.placed.append(dict(side="BUY", instrument_token=instrument_token, quantity=quantity,
                                 order_type=order_type, tag=tag, price=price, trigger_price=trigger_price))
        return f"BUY-{len(self.placed)}"


class TestLegTriggered:
    def test_market_always_triggers(self):
        assert leg_triggered({"order_type": "MARKET", "transaction_type": "BUY"}, 100) is True

    def test_sell_limit_triggers_when_ltp_at_or_above_price(self):
        leg = {"order_type": "LIMIT", "transaction_type": "SELL", "price": 100}
        assert leg_triggered(leg, 101) is True
        assert leg_triggered(leg, 100) is True
        assert leg_triggered(leg, 99) is False

    def test_buy_limit_triggers_when_ltp_at_or_below_price(self):
        leg = {"order_type": "LIMIT", "transaction_type": "BUY", "price": 100}
        assert leg_triggered(leg, 99) is True
        assert leg_triggered(leg, 100) is True
        assert leg_triggered(leg, 101) is False

    def test_sell_stop_triggers_when_ltp_at_or_below_trigger(self):
        # Protecting a long — exits via SELL when price falls to the stop.
        leg = {"order_type": "SL-M", "transaction_type": "SELL", "trigger_price": 95}
        assert leg_triggered(leg, 94) is True
        assert leg_triggered(leg, 95) is True
        assert leg_triggered(leg, 96) is False

    def test_buy_stop_triggers_when_ltp_at_or_above_trigger(self):
        # Covering a short — exits via BUY when price rises to the stop.
        leg = {"order_type": "SL-M", "transaction_type": "BUY", "trigger_price": 105}
        assert leg_triggered(leg, 106) is True
        assert leg_triggered(leg, 105) is True
        assert leg_triggered(leg, 104) is False

    def test_unknown_order_type_never_triggers(self):
        assert leg_triggered({"order_type": "GTT", "transaction_type": "BUY"}, 100) is False


class TestPlaceLeg:
    def test_sell_leg_calls_place_sell_order(self):
        broker = FakeBroker()
        leg = {"instrument_token": "X", "transaction_type": "SELL", "quantity": 10,
               "order_type": "LIMIT", "price": 50, "trigger_price": 0, "product": "D"}
        order_id = place_leg(broker, leg, "TAG_TOO_LONG_FOR_16_CHARS", user_id=7)
        assert order_id == "SELL-1"
        assert broker.placed[0]["side"] == "SELL"
        assert broker.placed[0]["price"] == 50
        assert len(broker.placed[0]["tag"]) <= 16

    def test_buy_leg_calls_place_buy_order(self):
        broker = FakeBroker()
        leg = {"instrument_token": "X", "transaction_type": "BUY", "quantity": 10,
               "order_type": "SL-M", "price": 0, "trigger_price": 45, "product": "D"}
        order_id = place_leg(broker, leg, "TAG", user_id=7)
        assert order_id == "BUY-1"
        assert broker.placed[0]["side"] == "BUY"
        assert broker.placed[0]["trigger_price"] == 45


class TestSimulatePaperFill:
    def test_always_places_market_regardless_of_leg_order_type(self):
        broker = FakeBroker()
        leg = {"instrument_token": "X", "transaction_type": "SELL", "quantity": 5, "order_type": "LIMIT", "product": "D"}
        simulate_paper_fill(broker, leg, "TAG", user_id=1)
        assert broker.placed[0]["order_type"] == "MARKET"


class TestValidateLeg:
    def test_rejects_non_positive_quantity(self):
        broker = FakeBroker(ltp={"X": 100})
        leg = {"instrument_token": "X", "transaction_type": "BUY", "quantity": 0, "order_type": "MARKET"}
        assert validate_leg(broker, leg) is not None

    def test_rejects_unknown_instrument(self):
        broker = FakeBroker(ltp={})
        leg = {"instrument_token": "UNKNOWN", "transaction_type": "BUY", "quantity": 1, "order_type": "MARKET"}
        err = validate_leg(broker, leg)
        assert err is not None
        assert "UNKNOWN" in err

    def test_rejects_limit_order_with_no_price(self):
        broker = FakeBroker(ltp={"X": 100})
        leg = {"instrument_token": "X", "transaction_type": "BUY", "quantity": 1, "order_type": "LIMIT", "price": 0}
        assert validate_leg(broker, leg) is not None

    def test_rejects_slm_order_with_no_trigger(self):
        broker = FakeBroker(ltp={"X": 100})
        leg = {"instrument_token": "X", "transaction_type": "BUY", "quantity": 1, "order_type": "SL-M", "trigger_price": 0}
        assert validate_leg(broker, leg) is not None

    def test_rejects_sl_order_with_no_price(self):
        broker = FakeBroker(ltp={"X": 100})
        leg = {"instrument_token": "X", "transaction_type": "BUY", "quantity": 1, "order_type": "SL", "trigger_price": 90, "price": 0}
        assert validate_leg(broker, leg) is not None

    def test_accepts_valid_market_leg(self):
        broker = FakeBroker(ltp={"X": 100})
        leg = {"instrument_token": "X", "transaction_type": "BUY", "quantity": 1, "order_type": "MARKET"}
        assert validate_leg(broker, leg) is None

    def test_accepts_valid_limit_leg(self):
        broker = FakeBroker(ltp={"X": 100})
        leg = {"instrument_token": "X", "transaction_type": "SELL", "quantity": 1, "order_type": "LIMIT", "price": 105}
        assert validate_leg(broker, leg) is None
