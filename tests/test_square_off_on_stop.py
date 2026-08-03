"""
tests/test_square_off_on_stop.py — custom_strategy_scheduler.py::
square_off_all_open_legs, added to close a real gap: the frontend's Stop
confirmation dialog promises "square off any open position," but
update_strategy_status previously only flipped the status column —
leaving any open leg (a real broker position, for LIVE) unmanaged
forever, since the scheduler's main loop excludes non PAPER_TRADING/LIVE
strategies. Each leg must be closed via ITS OWN entry mode (leg.mode),
not the strategy's current status, since a strategy can be
LIVE -> PAUSED -> STOPPED while its already-open legs stay 'live'.
"""
import json

import pytest

from automate.db.models import CustomStrategy, CustomStrategyPosition
from automate.api.custom_strategy_scheduler import square_off_all_open_legs


@pytest.fixture()
def db(db_session):
    return db_session


def _make_strategy(db, **overrides):
    defaults = dict(
        user_id=1, name="Live Strangle", instrument_type="INDEX", strategy_type="CUSTOM", option_type="BOTH",
        symbols=json.dumps(["NIFTY"]), status="LIVE",
    )
    defaults.update(overrides)
    s = CustomStrategy(**defaults)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _make_leg(strategy_id, instrument_key, mode, transaction_type="SELL", quantity=50, status="OPEN"):
    return CustomStrategyPosition(
        strategy_id=strategy_id, leg_index=0, mode=mode, instrument_key=instrument_key,
        instrument_type="OPTION", option_type="CE", strike=24000, expiry="2026-08-27",
        transaction_type=transaction_type, quantity=quantity, entry_price=100.0, status=status,
    )


class FakeBroker:
    def __init__(self, ltp=90.0, fail=False):
        self._ltp = ltp
        self._fail = fail
        self.orders_placed = []

    def get_ltp_batch(self, tokens):
        return {t: self._ltp for t in tokens}

    def place_buy_order(self, instrument_token, quantity, order_type, tag, user_id=None):
        if self._fail:
            raise RuntimeError("broker rejected order")
        self.orders_placed.append(("BUY", instrument_token, quantity))
        return "ORD1"

    def place_sell_order(self, instrument_token, quantity, order_type, tag, user_id=None):
        if self._fail:
            raise RuntimeError("broker rejected order")
        self.orders_placed.append(("SELL", instrument_token, quantity))
        return "ORD1"

    def get_fill_price(self, order_id):
        return None


class TestSquareOffAllOpenLegs:
    def test_no_open_legs_is_a_noop(self, db):
        s = _make_strategy(db)
        assert square_off_all_open_legs(db, s, {"live": FakeBroker()}) == 0

    def test_closes_a_live_leg_via_the_live_broker(self, db):
        s = _make_strategy(db, status="LIVE")
        leg = _make_leg(s.id, "NSE_FO|1", mode="live")
        db.add(leg)
        db.commit()

        live_broker = FakeBroker()
        closed = square_off_all_open_legs(db, s, {"live": live_broker, "paper": FakeBroker()})

        assert closed == 1
        db.refresh(leg)
        assert leg.status == "CLOSED"
        assert leg.exit_reason == "MANUAL_STOP"
        assert leg.exit_order_id == "ORD1"
        assert live_broker.orders_placed == [("BUY", "NSE_FO|1", 50)]  # opposite of SELL entry

    def test_closes_a_paper_leg_via_the_paper_broker(self, db):
        s = _make_strategy(db, status="PAPER_TRADING")
        leg = _make_leg(s.id, "NSE_FO|1", mode="paper")
        db.add(leg)
        db.commit()

        paper_broker = FakeBroker()
        closed = square_off_all_open_legs(db, s, {"live": FakeBroker(), "paper": paper_broker})

        assert closed == 1
        assert paper_broker.orders_placed == [("BUY", "NSE_FO|1", 50)]

    def test_uses_the_legs_own_mode_not_the_strategy_status(self, db):
        """A strategy that was LIVE then PAUSED then STOPPED: its already-open leg is still mode='live'."""
        s = _make_strategy(db, status="PAUSED")
        leg = _make_leg(s.id, "NSE_FO|1", mode="live")
        db.add(leg)
        db.commit()

        live_broker = FakeBroker()
        closed = square_off_all_open_legs(db, s, {"live": live_broker, "paper": FakeBroker()})

        assert closed == 1
        assert live_broker.orders_placed == [("BUY", "NSE_FO|1", 50)]

    def test_mixed_mode_legs_each_use_their_own_broker(self, db):
        s = _make_strategy(db)
        live_leg = _make_leg(s.id, "NSE_FO|1", mode="live")
        paper_leg = _make_leg(s.id, "NSE_FO|2", mode="paper")
        db.add_all([live_leg, paper_leg])
        db.commit()

        live_broker, paper_broker = FakeBroker(), FakeBroker()
        closed = square_off_all_open_legs(db, s, {"live": live_broker, "paper": paper_broker})

        assert closed == 2
        assert live_broker.orders_placed == [("BUY", "NSE_FO|1", 50)]
        assert paper_broker.orders_placed == [("BUY", "NSE_FO|2", 50)]

    def test_missing_broker_for_a_mode_leaves_that_leg_open_and_alerts(self, db, monkeypatch):
        notified = []
        monkeypatch.setattr("automate.api.custom_strategy_scheduler.notify", lambda *a, **k: notified.append((a, k)))
        s = _make_strategy(db)
        leg = _make_leg(s.id, "NSE_FO|1", mode="live")
        db.add(leg)
        db.commit()

        closed = square_off_all_open_legs(db, s, {"paper": FakeBroker()})  # no "live" key

        assert closed == 0
        db.refresh(leg)
        assert leg.status == "OPEN"
        assert len(notified) == 1
        assert notified[0][1]["level"] == "error"

    def test_broker_order_failure_leaves_leg_open_and_alerts(self, db, monkeypatch):
        notified = []
        monkeypatch.setattr("automate.api.custom_strategy_scheduler.notify", lambda *a, **k: notified.append((a, k)))
        s = _make_strategy(db)
        leg = _make_leg(s.id, "NSE_FO|1", mode="live")
        db.add(leg)
        db.commit()

        closed = square_off_all_open_legs(db, s, {"live": FakeBroker(fail=True)})

        assert closed == 0
        db.refresh(leg)
        assert leg.status == "OPEN"
        assert len(notified) == 1

    def test_only_open_legs_are_touched_closed_ones_are_left_alone(self, db):
        s = _make_strategy(db)
        open_leg = _make_leg(s.id, "NSE_FO|1", mode="live", status="OPEN")
        closed_leg = _make_leg(s.id, "NSE_FO|2", mode="live", status="CLOSED")
        db.add_all([open_leg, closed_leg])
        db.commit()

        live_broker = FakeBroker()
        closed = square_off_all_open_legs(db, s, {"live": live_broker})

        assert closed == 1
        assert live_broker.orders_placed == [("BUY", "NSE_FO|1", 50)]
