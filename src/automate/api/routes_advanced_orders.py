"""
api/routes_advanced_orders.py — Advanced order types API.

Supports OCO (One-Cancels-Other), trailing stops, and bracket orders for
sophisticated trading strategies. Each row is backed by db.models.AdvancedOrder
and driven each tick by advanced_orders_scheduler.py — see that module's
docstring for exactly how 'live' vs 'paper' mode differ (live places real
resting orders at the broker immediately; paper evaluates the same
trigger logic against LTP on the scheduler's tick since PaperBroker has no
resting-order concept of its own).
"""
import json
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from automate.api.advanced_orders_common import OrderLeg, place_leg, validate_leg
from automate.api.auth import get_current_user
from automate.db.engine import get_db
from automate.db.models import AdvancedOrder

router = APIRouter(prefix="/api/orders/advanced", tags=["advanced-orders"])


def _current_user_id(user: dict) -> int:
    return int(user["sub"])


def _new_public_id(prefix: str) -> str:
    return f"{prefix}_{datetime.now().strftime('%Y%m%d%H%M%S')}{uuid.uuid4().hex[:4]}"


def _get_brokers():
    # Reuse the same lazily-built, retried-every-call broker pair the
    # custom-strategy scheduler uses — see that module for why this isn't
    # duplicated (a valid Upstox token may not be ready yet at any given
    # moment, and construction is retried rather than cached-as-failed).
    from automate.api.custom_strategy_scheduler import _get_brokers as _shared_get_brokers
    return _shared_get_brokers()


def _owned(db: Session, public_id: str, kind: str, user_id: int) -> AdvancedOrder:
    order = db.query(AdvancedOrder).filter(
        AdvancedOrder.public_id == public_id,
        AdvancedOrder.kind == kind,
        AdvancedOrder.user_id == user_id,
    ).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


class OCOOrderRequest(BaseModel):
    """One-Cancels-Other order request."""
    mode: str  # 'paper' | 'live'
    primary_order: OrderLeg
    secondary_order: OrderLeg
    strategy_name: Optional[str] = None


class TrailingStopRequest(BaseModel):
    """Trailing stop order request."""
    mode: str  # 'paper' | 'live'
    instrument_token: str
    symbol: str
    side: str  # "BUY" or "SELL" — direction of the ORIGINAL position being protected
    quantity: int
    trail_amount: float  # Amount to trail by (points or percentage)
    trail_type: str  # "points" or "percentage"
    product: str = "D"
    strategy_name: Optional[str] = None


class BracketOrderRequest(BaseModel):
    """Bracket order with entry, take-profit, and stop-loss."""
    mode: str  # 'paper' | 'live'
    entry_order: OrderLeg
    take_profit: OrderLeg
    stop_loss: OrderLeg
    strategy_name: Optional[str] = None


def _require_mode(mode: str) -> str:
    if mode not in ("paper", "live"):
        raise HTTPException(status_code=400, detail="mode must be 'paper' or 'live'")
    return mode


@router.post("/oco")
def create_oco_order(req: OCOOrderRequest, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """
    Create a One-Cancels-Other order pair.

    mode='live': both legs are placed as real resting orders at the broker
    immediately. mode='paper': both legs start PENDING and are evaluated
    against live LTP by advanced_orders_scheduler.py.

    When one leg fills (or, in paper mode, its trigger condition is met),
    the other is automatically cancelled.
    """
    _require_mode(req.mode)
    public_id = _new_public_id("oco")
    primary = req.primary_order.model_dump()
    secondary = req.secondary_order.model_dump()
    primary["status"] = "PENDING"
    secondary["status"] = "PENDING"

    brokers = _get_brokers()
    if brokers is None:
        raise HTTPException(status_code=503, detail="Broker not ready — try again shortly.")
    for leg in (primary, secondary):
        err = validate_leg(brokers["paper"], leg)
        if err:
            raise HTTPException(status_code=400, detail=err)

    if req.mode == "live":
        broker = brokers["live"]
        try:
            primary["order_id"] = place_leg(broker, primary, f"OCO_{public_id}", _current_user_id(user))
            primary["status"] = "PLACED"
            secondary["order_id"] = place_leg(broker, secondary, f"OCO_{public_id}", _current_user_id(user))
            secondary["status"] = "PLACED"
        except Exception as exc:
            # Don't leave a naked single-leg "OCO" resting at the broker —
            # if the second leg failed to place, cancel whichever leg did
            # place before surfacing the error.
            if primary.get("order_id"):
                broker.cancel_order(primary["order_id"])
            raise HTTPException(status_code=502, detail=f"Failed to place OCO order pair: {exc}")

    order = AdvancedOrder(
        public_id=public_id, kind="OCO", user_id=_current_user_id(user), mode=req.mode,
        strategy_name=req.strategy_name, status="ACTIVE",
        state_json=json.dumps({"primary_order": primary, "secondary_order": secondary}),
    )
    db.add(order)
    db.commit()

    return {"oco_id": public_id, "status": "created", "message": "OCO order pair created successfully"}


@router.get("/oco/{oco_id}")
def get_oco_order(oco_id: str, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """Get OCO order details by ID."""
    return _owned(db, oco_id, "OCO", _current_user_id(user)).to_dict()


@router.delete("/oco/{oco_id}")
def cancel_oco_order(oco_id: str, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """Cancel an OCO order pair — cancels any leg still resting at the broker."""
    order = _owned(db, oco_id, "OCO", _current_user_id(user))
    state = json.loads(order.state_json)

    if order.mode == "live":
        brokers = _get_brokers()
        broker = brokers["live"] if brokers else None
        for key in ("primary_order", "secondary_order"):
            leg = state[key]
            if leg["status"] == "PLACED" and leg.get("order_id") and broker is not None:
                broker.cancel_order(leg["order_id"])
            leg["status"] = "CANCELLED"
    else:
        for key in ("primary_order", "secondary_order"):
            state[key]["status"] = "CANCELLED"

    order.status = "CANCELLED"
    order.state_json = json.dumps(state)
    db.commit()

    return {"status": "cancelled", "oco_id": oco_id}


@router.post("/trailing-stop")
def create_trailing_stop(req: TrailingStopRequest, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """
    Create a trailing stop order.

    The stop price adjusts dynamically as the market moves in your favor —
    advanced_orders_scheduler.py recomputes it every tick and, in live
    mode, pushes the new trigger to the broker via modify_order() so a
    real order is always resting at the exchange.
    """
    _require_mode(req.mode)
    if req.quantity <= 0:
        raise HTTPException(status_code=400, detail=f"quantity must be positive, got {req.quantity}")
    if req.trail_amount <= 0:
        raise HTTPException(status_code=400, detail=f"trail_amount must be positive, got {req.trail_amount}")
    if req.side not in ("BUY", "SELL"):
        raise HTTPException(status_code=400, detail="side must be 'BUY' or 'SELL'")
    if req.trail_type not in ("points", "percentage"):
        raise HTTPException(status_code=400, detail="trail_type must be 'points' or 'percentage'")

    brokers = _get_brokers()
    if brokers is None:
        raise HTTPException(status_code=503, detail="Broker not ready — try again shortly.")
    if brokers["paper"].get_ltp(req.instrument_token) is None:
        raise HTTPException(status_code=400, detail=f"instrument_token '{req.instrument_token}' not recognized (no LTP available)")

    public_id = _new_public_id("ts")

    state = {
        "symbol": req.symbol,
        "instrument_token": req.instrument_token,
        "side": req.side,
        "quantity": req.quantity,
        "trail_amount": req.trail_amount,
        "trail_type": req.trail_type,
        "product": req.product,
        "current_stop_price": None,
        "highest_price": None,
        "lowest_price": None,
        "broker_order_id": None,
        "exit_order_id": None,
        "exit_price": None,
    }
    # Initial placement/trigger computation happens on the next scheduler
    # tick (needs a live LTP quote) rather than here, so both modes go
    # through exactly one code path for that logic.

    order = AdvancedOrder(
        public_id=public_id, kind="TRAILING_STOP", user_id=_current_user_id(user), mode=req.mode,
        strategy_name=req.strategy_name, status="ACTIVE", state_json=json.dumps(state),
    )
    db.add(order)
    db.commit()

    return {"ts_id": public_id, "status": "created", "message": "Trailing stop order created successfully"}


@router.get("/trailing-stop/{ts_id}")
def get_trailing_stop(ts_id: str, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """Get trailing stop order details."""
    return _owned(db, ts_id, "TRAILING_STOP", _current_user_id(user)).to_dict()


@router.delete("/trailing-stop/{ts_id}")
def cancel_trailing_stop(ts_id: str, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """Cancel a trailing stop order — cancels the resting broker order, if any."""
    order = _owned(db, ts_id, "TRAILING_STOP", _current_user_id(user))
    state = json.loads(order.state_json)

    if order.mode == "live" and state.get("broker_order_id"):
        brokers = _get_brokers()
        if brokers:
            brokers["live"].cancel_order(state["broker_order_id"])

    order.status = "CANCELLED"
    db.commit()

    return {"status": "cancelled", "ts_id": ts_id}


@router.post("/bracket")
def create_bracket_order(req: BracketOrderRequest, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """
    Create a bracket order with entry, take-profit, and stop-loss.

    The entry order is placed immediately (live: a real broker order;
    paper: filled instantly, matching how PaperBroker behaves everywhere
    else in this codebase). Once the entry is filled,
    advanced_orders_scheduler.py places the take-profit/stop-loss pair and
    then manages them exactly like an OCO pair.
    """
    _require_mode(req.mode)
    public_id = _new_public_id("bracket")
    entry = req.entry_order.model_dump()
    tp = req.take_profit.model_dump()
    sl = req.stop_loss.model_dump()
    entry["status"] = "PENDING"
    tp["status"] = "PENDING"
    sl["status"] = "PENDING"

    brokers = _get_brokers()
    if brokers is None:
        raise HTTPException(status_code=503, detail="Broker not ready — try again shortly.")
    for leg in (entry, tp, sl):
        err = validate_leg(brokers["paper"], leg)
        if err:
            raise HTTPException(status_code=400, detail=err)

    if req.mode == "live":
        broker = brokers["live"]
        try:
            entry["order_id"] = place_leg(broker, entry, f"BRK_{public_id}", _current_user_id(user))
            entry["status"] = "PLACED"
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Failed to place bracket entry order: {exc}")
    else:
        broker = brokers["paper"]
        try:
            entry["order_id"] = place_leg(broker, {**entry, "order_type": "MARKET"}, f"BRK_{public_id}", _current_user_id(user))
            entry["status"] = "COMPLETE"
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Failed to fill bracket entry order: {exc}")

    order = AdvancedOrder(
        public_id=public_id, kind="BRACKET", user_id=_current_user_id(user), mode=req.mode,
        strategy_name=req.strategy_name, status="ACTIVE",
        state_json=json.dumps({"entry_order": entry, "take_profit": tp, "stop_loss": sl}),
    )
    db.add(order)
    db.commit()

    return {"bracket_id": public_id, "status": "created", "message": "Bracket order created successfully"}


@router.get("/bracket/{bracket_id}")
def get_bracket_order(bracket_id: str, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """Get bracket order details."""
    return _owned(db, bracket_id, "BRACKET", _current_user_id(user)).to_dict()


@router.delete("/bracket/{bracket_id}")
def cancel_bracket_order(bracket_id: str, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """Cancel a bracket order — cancels any leg still resting at the broker."""
    order = _owned(db, bracket_id, "BRACKET", _current_user_id(user))
    state = json.loads(order.state_json)

    if order.mode == "live":
        brokers = _get_brokers()
        broker = brokers["live"] if brokers else None
        for key in ("entry_order", "take_profit", "stop_loss"):
            leg = state[key]
            if leg["status"] == "PLACED" and leg.get("order_id") and broker is not None:
                broker.cancel_order(leg["order_id"])
            if leg["status"] != "COMPLETE":
                leg["status"] = "CANCELLED"
    else:
        for key in ("take_profit", "stop_loss"):
            if state[key]["status"] != "COMPLETE":
                state[key]["status"] = "CANCELLED"

    order.status = "CANCELLED"
    order.state_json = json.dumps(state)
    db.commit()

    return {"status": "cancelled", "bracket_id": bracket_id}


@router.get("/list")
def list_advanced_orders(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """List all advanced orders owned by the caller."""
    user_id = _current_user_id(user)
    orders = db.query(AdvancedOrder).filter(AdvancedOrder.user_id == user_id).all()
    return {
        "oco_orders": [o.to_dict() for o in orders if o.kind == "OCO"],
        "trailing_stops": [o.to_dict() for o in orders if o.kind == "TRAILING_STOP"],
        "bracket_orders": [o.to_dict() for o in orders if o.kind == "BRACKET"],
    }
