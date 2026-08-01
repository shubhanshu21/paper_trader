"""
tests/test_routes_advanced_orders.py — Tests for routes_advanced_orders.py's
endpoint functions, called directly as plain Python functions (FastAPI's
Depends() defaults are just sentinels unless invoked through the ASGI
app — passing real db/user objects positionally/by-keyword bypasses that
machinery entirely, consistent with this repo's existing test style of
testing business logic directly rather than through HTTP).

Uses a throwaway in-memory SQLite database (see tests/test_position_tracker.py
for the same pattern) — no shared state with the production MySQL database.
"""
import json

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.types import BigInteger


@compiles(BigInteger, "sqlite")
def compile_bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"


from automate.db.engine import Base
from automate.db.models import AdvancedOrder
import automate.api.routes_advanced_orders as routes
from automate.api.routes_advanced_orders import (
    BracketOrderRequest, OCOOrderRequest, TrailingStopRequest,
    cancel_bracket_order, cancel_oco_order, cancel_trailing_stop,
    create_bracket_order, create_oco_order, create_trailing_stop,
    get_bracket_order, get_oco_order, get_trailing_stop, list_advanced_orders,
)


class FakeBroker:
    def __init__(self, ltp=None, fail=False):
        self.ltp = ltp or {}
        self.fail = fail
        self.placed = []
        self.cancelled = []

    def get_ltp(self, instrument_token):
        return self.ltp.get(instrument_token)

    def place_sell_order(self, instrument_token, quantity, product="D", order_type="MARKET",
                          tag="", user_id=None, price=0, trigger_price=0):
        if self.fail:
            raise RuntimeError("simulated broker rejection")
        oid = f"SELL-{len(self.placed) + 1}"
        self.placed.append(oid)
        return oid

    def place_buy_order(self, instrument_token, quantity, product="D", order_type="MARKET",
                         tag="", user_id=None, price=0, trigger_price=0):
        if self.fail:
            raise RuntimeError("simulated broker rejection")
        oid = f"BUY-{len(self.placed) + 1}"
        self.placed.append(oid)
        return oid

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return True


USER = {"sub": "1"}
OTHER_USER = {"sub": "2"}


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[AdvancedOrder.__table__])
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


@pytest.fixture()
def brokers(monkeypatch):
    fake = {"paper": FakeBroker(ltp={"X": 100}), "live": FakeBroker(ltp={"X": 100})}
    monkeypatch.setattr(routes, "_get_brokers", lambda: fake)
    return fake


class TestCreateOco:
    def test_paper_oco_creates_active_row_with_pending_legs(self, db, brokers):
        req = OCOOrderRequest(
            mode="paper",
            primary_order={"instrument_token": "X", "transaction_type": "SELL", "quantity": 1,
                            "order_type": "LIMIT", "price": 110},
            secondary_order={"instrument_token": "X", "transaction_type": "BUY", "quantity": 1,
                              "order_type": "SL-M", "trigger_price": 90},
        )
        resp = create_oco_order(req, db, USER)
        assert resp["status"] == "created"

        row = db.query(AdvancedOrder).filter(AdvancedOrder.public_id == resp["oco_id"]).first()
        assert row.status == "ACTIVE"
        assert row.mode == "paper"
        state = json.loads(row.state_json)
        assert state["primary_order"]["status"] == "PENDING"
        assert brokers["paper"].placed == []  # nothing placed yet in paper mode

    def test_live_oco_places_both_legs_immediately(self, db, brokers):
        req = OCOOrderRequest(
            mode="live",
            primary_order={"instrument_token": "X", "transaction_type": "SELL", "quantity": 1,
                            "order_type": "LIMIT", "price": 110},
            secondary_order={"instrument_token": "X", "transaction_type": "BUY", "quantity": 1,
                              "order_type": "SL-M", "trigger_price": 90},
        )
        resp = create_oco_order(req, db, USER)

        row = db.query(AdvancedOrder).filter(AdvancedOrder.public_id == resp["oco_id"]).first()
        state = json.loads(row.state_json)
        assert state["primary_order"]["status"] == "PLACED"
        assert state["secondary_order"]["status"] == "PLACED"
        assert len(brokers["live"].placed) == 2

    def test_bad_instrument_rejected_before_any_placement(self, db, brokers):
        req = OCOOrderRequest(
            mode="live",
            primary_order={"instrument_token": "UNKNOWN", "transaction_type": "SELL", "quantity": 1,
                            "order_type": "LIMIT", "price": 110},
            secondary_order={"instrument_token": "X", "transaction_type": "BUY", "quantity": 1,
                              "order_type": "SL-M", "trigger_price": 90},
        )
        with pytest.raises(HTTPException) as exc_info:
            create_oco_order(req, db, USER)
        assert exc_info.value.status_code == 400
        assert brokers["live"].placed == []
        assert db.query(AdvancedOrder).count() == 0

    def test_invalid_mode_rejected(self, db, brokers):
        req = OCOOrderRequest(
            mode="not_a_mode",
            primary_order={"instrument_token": "X", "transaction_type": "SELL", "quantity": 1},
            secondary_order={"instrument_token": "X", "transaction_type": "BUY", "quantity": 1},
        )
        with pytest.raises(HTTPException) as exc_info:
            create_oco_order(req, db, USER)
        assert exc_info.value.status_code == 400

    def test_live_secondary_leg_failure_cancels_primary(self, db, brokers):
        req = OCOOrderRequest(
            mode="live",
            primary_order={"instrument_token": "X", "transaction_type": "SELL", "quantity": 1,
                            "order_type": "LIMIT", "price": 110},
            secondary_order={"instrument_token": "X", "transaction_type": "BUY", "quantity": 1,
                              "order_type": "SL-M", "trigger_price": 90},
        )
        real_place_buy = brokers["live"].place_buy_order
        brokers["live"].place_sell_order = lambda *a, **k: "SELL-1"

        def failing_buy(*a, **k):
            raise RuntimeError("simulated secondary-leg rejection")
        brokers["live"].place_buy_order = failing_buy

        with pytest.raises(HTTPException) as exc_info:
            create_oco_order(req, db, USER)
        assert exc_info.value.status_code == 502
        assert brokers["live"].cancelled == ["SELL-1"]
        assert db.query(AdvancedOrder).count() == 0  # never persisted — request failed


class TestOwnershipIsolation:
    def test_other_user_cannot_read_oco_order(self, db, brokers):
        req = OCOOrderRequest(
            mode="paper",
            primary_order={"instrument_token": "X", "transaction_type": "SELL", "quantity": 1, "order_type": "MARKET"},
            secondary_order={"instrument_token": "X", "transaction_type": "BUY", "quantity": 1, "order_type": "MARKET"},
        )
        resp = create_oco_order(req, db, USER)

        with pytest.raises(HTTPException) as exc_info:
            get_oco_order(resp["oco_id"], db, OTHER_USER)
        assert exc_info.value.status_code == 404

        # Owner can read it fine.
        assert get_oco_order(resp["oco_id"], db, USER)["kind"] == "OCO"

    def test_list_only_returns_caller_own_orders(self, db, brokers):
        req = OCOOrderRequest(
            mode="paper",
            primary_order={"instrument_token": "X", "transaction_type": "SELL", "quantity": 1, "order_type": "MARKET"},
            secondary_order={"instrument_token": "X", "transaction_type": "BUY", "quantity": 1, "order_type": "MARKET"},
        )
        create_oco_order(req, db, USER)
        create_oco_order(req, db, OTHER_USER)

        result = list_advanced_orders(db, USER)
        assert len(result["oco_orders"]) == 1


class TestCancelOco:
    def test_cancel_live_oco_cancels_both_resting_legs(self, db, brokers):
        req = OCOOrderRequest(
            mode="live",
            primary_order={"instrument_token": "X", "transaction_type": "SELL", "quantity": 1,
                            "order_type": "LIMIT", "price": 110},
            secondary_order={"instrument_token": "X", "transaction_type": "BUY", "quantity": 1,
                              "order_type": "SL-M", "trigger_price": 90},
        )
        resp = create_oco_order(req, db, USER)
        brokers["live"].cancelled = []  # reset after creation

        cancel_resp = cancel_oco_order(resp["oco_id"], db, USER)
        assert cancel_resp["status"] == "cancelled"
        assert len(brokers["live"].cancelled) == 2

        row = db.query(AdvancedOrder).filter(AdvancedOrder.public_id == resp["oco_id"]).first()
        assert row.status == "CANCELLED"


class TestCreateTrailingStop:
    def test_creates_with_no_broker_call_yet(self, db, brokers):
        req = TrailingStopRequest(mode="paper", instrument_token="X", symbol="X", side="BUY",
                                   quantity=1, trail_amount=5, trail_type="points")
        resp = create_trailing_stop(req, db, USER)
        assert resp["status"] == "created"
        assert brokers["paper"].placed == []

    def test_rejects_non_positive_quantity(self, db, brokers):
        req = TrailingStopRequest(mode="paper", instrument_token="X", symbol="X", side="BUY",
                                   quantity=0, trail_amount=5, trail_type="points")
        with pytest.raises(HTTPException) as exc_info:
            create_trailing_stop(req, db, USER)
        assert exc_info.value.status_code == 400

    def test_rejects_bad_instrument(self, db, brokers):
        req = TrailingStopRequest(mode="paper", instrument_token="NOPE", symbol="X", side="BUY",
                                   quantity=1, trail_amount=5, trail_type="points")
        with pytest.raises(HTTPException) as exc_info:
            create_trailing_stop(req, db, USER)
        assert exc_info.value.status_code == 400


class TestCreateBracket:
    def test_paper_entry_fills_immediately(self, db, brokers):
        req = BracketOrderRequest(
            mode="paper",
            entry_order={"instrument_token": "X", "transaction_type": "SELL", "quantity": 1, "order_type": "MARKET"},
            take_profit={"instrument_token": "X", "transaction_type": "BUY", "quantity": 1,
                         "order_type": "LIMIT", "price": 80},
            stop_loss={"instrument_token": "X", "transaction_type": "BUY", "quantity": 1,
                       "order_type": "SL-M", "trigger_price": 120},
        )
        resp = create_bracket_order(req, db, USER)
        row = db.query(AdvancedOrder).filter(AdvancedOrder.public_id == resp["bracket_id"]).first()
        state = json.loads(row.state_json)
        assert state["entry_order"]["status"] == "COMPLETE"
        assert state["take_profit"]["status"] == "PENDING"

    def test_live_entry_placed_not_complete(self, db, brokers):
        req = BracketOrderRequest(
            mode="live",
            entry_order={"instrument_token": "X", "transaction_type": "SELL", "quantity": 1,
                         "order_type": "LIMIT", "price": 100},
            take_profit={"instrument_token": "X", "transaction_type": "BUY", "quantity": 1,
                         "order_type": "LIMIT", "price": 80},
            stop_loss={"instrument_token": "X", "transaction_type": "BUY", "quantity": 1,
                       "order_type": "SL-M", "trigger_price": 120},
        )
        resp = create_bracket_order(req, db, USER)
        row = db.query(AdvancedOrder).filter(AdvancedOrder.public_id == resp["bracket_id"]).first()
        state = json.loads(row.state_json)
        assert state["entry_order"]["status"] == "PLACED"

    def test_entry_placement_failure_returns_502_and_persists_nothing(self, db, brokers):
        brokers["live"].fail = True
        req = BracketOrderRequest(
            mode="live",
            entry_order={"instrument_token": "X", "transaction_type": "SELL", "quantity": 1,
                         "order_type": "LIMIT", "price": 100},
            take_profit={"instrument_token": "X", "transaction_type": "BUY", "quantity": 1,
                         "order_type": "LIMIT", "price": 80},
            stop_loss={"instrument_token": "X", "transaction_type": "BUY", "quantity": 1,
                       "order_type": "SL-M", "trigger_price": 120},
        )
        with pytest.raises(HTTPException) as exc_info:
            create_bracket_order(req, db, USER)
        assert exc_info.value.status_code == 502
        assert db.query(AdvancedOrder).count() == 0
