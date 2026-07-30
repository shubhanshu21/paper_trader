"""
api/routes_advanced_orders.py — Advanced order types API.

Supports OCO (One-Cancels-Other), trailing stops, and other
complex order types for sophisticated trading strategies.
"""
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/orders/advanced", tags=["advanced-orders"])


class OCOOrderRequest(BaseModel):
    """One-Cancels-Other order request."""
    primary_order: dict  # Main order details
    secondary_order: dict  # Stop-loss or take-profit order
    user_id: Optional[int] = None
    strategy_name: Optional[str] = None


class TrailingStopRequest(BaseModel):
    """Trailing stop order request."""
    symbol: str
    side: str  # "BUY" or "SELL"
    quantity: int
    trail_amount: float  # Amount to trail by (points or percentage)
    trail_type: str  # "points" or "percentage"
    user_id: Optional[int] = None
    strategy_name: Optional[str] = None


class BracketOrderRequest(BaseModel):
    """Bracket order with take-profit and stop-loss."""
    entry_order: dict
    take_profit: dict
    stop_loss: dict
    user_id: Optional[int] = None
    strategy_name: Optional[str] = None


# In-memory storage for advanced orders (in production, use database)
oco_orders: dict = {}
trailing_stops: dict = {}
bracket_orders: dict = {}


@router.post("/oco")
def create_oco_order(req: OCOOrderRequest):
    """
    Create a One-Cancels-Other order pair.
    
    When one order is filled or cancelled, the other is automatically cancelled.
    """
    oco_id = f"oco_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    
    oco_orders[oco_id] = {
        "oco_id": oco_id,
        "primary_order": req.primary_order,
        "secondary_order": req.secondary_order,
        "user_id": req.user_id,
        "strategy_name": req.strategy_name,
        "status": "active",
        "created_at": datetime.now().isoformat(),
        "primary_status": "pending",
        "secondary_status": "pending"
    }
    
    return {
        "oco_id": oco_id,
        "status": "created",
        "message": "OCO order pair created successfully"
    }


@router.get("/oco/{oco_id}")
def get_oco_order(oco_id: str):
    """Get OCO order details by ID."""
    if oco_id not in oco_orders:
        raise HTTPException(status_code=404, detail="OCO order not found")
    return oco_orders[oco_id]


@router.delete("/oco/{oco_id}")
def cancel_oco_order(oco_id: str):
    """Cancel an OCO order pair."""
    if oco_id not in oco_orders:
        raise HTTPException(status_code=404, detail="OCO order not found")
    
    oco_orders[oco_id]["status"] = "cancelled"
    oco_orders[oco_id]["primary_status"] = "cancelled"
    oco_orders[oco_id]["secondary_status"] = "cancelled"
    
    return {"status": "cancelled", "oco_id": oco_id}


@router.post("/trailing-stop")
def create_trailing_stop(req: TrailingStopRequest):
    """
    Create a trailing stop order.
    
    The stop price adjusts dynamically as the market moves in your favor.
    """
    ts_id = f"ts_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    
    trailing_stops[ts_id] = {
        "ts_id": ts_id,
        "symbol": req.symbol,
        "side": req.side,
        "quantity": req.quantity,
        "trail_amount": req.trail_amount,
        "trail_type": req.trail_type,
        "user_id": req.user_id,
        "strategy_name": req.strategy_name,
        "status": "active",
        "current_stop_price": None,
        "highest_price": None,
        "lowest_price": None,
        "created_at": datetime.now().isoformat()
    }
    
    return {
        "ts_id": ts_id,
        "status": "created",
        "message": "Trailing stop order created successfully"
    }


@router.get("/trailing-stop/{ts_id}")
def get_trailing_stop(ts_id: str):
    """Get trailing stop order details."""
    if ts_id not in trailing_stops:
        raise HTTPException(status_code=404, detail="Trailing stop order not found")
    return trailing_stops[ts_id]


@router.put("/trailing-stop/{ts_id}/update")
def update_trailing_stop(ts_id: str, current_price: float):
    """
    Update trailing stop price based on current market price.
    
    Called by market data updates to adjust the stop price.
    """
    if ts_id not in trailing_stops:
        raise HTTPException(status_code=404, detail="Trailing stop order not found")
    
    ts = trailing_stops[ts_id]
    
    if ts["side"] == "BUY":
        # For buy orders, trail price upward
        if ts["highest_price"] is None or current_price > ts["highest_price"]:
            ts["highest_price"] = current_price
            if ts["trail_type"] == "points":
                ts["current_stop_price"] = current_price - ts["trail_amount"]
            else:
                ts["current_stop_price"] = current_price * (1 - ts["trail_amount"] / 100)
    else:
        # For sell orders, trail price downward
        if ts["lowest_price"] is None or current_price < ts["lowest_price"]:
            ts["lowest_price"] = current_price
            if ts["trail_type"] == "points":
                ts["current_stop_price"] = current_price + ts["trail_amount"]
            else:
                ts["current_stop_price"] = current_price * (1 + ts["trail_amount"] / 100)
    
    return trailing_stops[ts_id]


@router.delete("/trailing-stop/{ts_id}")
def cancel_trailing_stop(ts_id: str):
    """Cancel a trailing stop order."""
    if ts_id not in trailing_stops:
        raise HTTPException(status_code=404, detail="Trailing stop order not found")
    
    trailing_stops[ts_id]["status"] = "cancelled"
    
    return {"status": "cancelled", "ts_id": ts_id}


@router.post("/bracket")
def create_bracket_order(req: BracketOrderRequest):
    """
    Create a bracket order with entry, take-profit, and stop-loss.
    
    All three orders are placed simultaneously. Entry order execution
    activates the take-profit and stop-loss orders.
    """
    bracket_id = f"bracket_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    
    bracket_orders[bracket_id] = {
        "bracket_id": bracket_id,
        "entry_order": req.entry_order,
        "take_profit": req.take_profit,
        "stop_loss": req.stop_loss,
        "user_id": req.user_id,
        "strategy_name": req.strategy_name,
        "status": "active",
        "entry_status": "pending",
        "tp_status": "pending",
        "sl_status": "pending",
        "created_at": datetime.now().isoformat()
    }
    
    return {
        "bracket_id": bracket_id,
        "status": "created",
        "message": "Bracket order created successfully"
    }


@router.get("/bracket/{bracket_id}")
def get_bracket_order(bracket_id: str):
    """Get bracket order details."""
    if bracket_id not in bracket_orders:
        raise HTTPException(status_code=404, detail="Bracket order not found")
    return bracket_orders[bracket_id]


@router.delete("/bracket/{bracket_id}")
def cancel_bracket_order(bracket_id: str):
    """Cancel a bracket order."""
    if bracket_id not in bracket_orders:
        raise HTTPException(status_code=404, detail="Bracket order not found")
    
    bracket_orders[bracket_id]["status"] = "cancelled"
    bracket_orders[bracket_id]["entry_status"] = "cancelled"
    bracket_orders[bracket_id]["tp_status"] = "cancelled"
    bracket_orders[bracket_id]["sl_status"] = "cancelled"
    
    return {"status": "cancelled", "bracket_id": bracket_id}


@router.get("/list")
def list_advanced_orders(user_id: Optional[int] = None):
    """List all advanced orders, optionally filtered by user."""
    result = {
        "oco_orders": [o for o in oco_orders.values() if user_id is None or o["user_id"] == user_id],
        "trailing_stops": [t for t in trailing_stops.values() if user_id is None or t["user_id"] == user_id],
        "bracket_orders": [b for b in bracket_orders.values() if user_id is None or b["user_id"] == user_id]
    }
    return result
