"""
api/advanced_orders_common.py — shared leg schema/helpers for
routes_advanced_orders.py (creation) and advanced_orders_scheduler.py
(tick-driven monitoring), so both sides agree on exactly what a "leg"
looks like and how it gets placed/evaluated.
"""
from typing import Literal, Optional

from pydantic import BaseModel

from automate.utils.logger import get_logger

log = get_logger(__name__)


class OrderLeg(BaseModel):
    """
    One order leg — the placeable unit for OCO/trailing-stop/bracket
    orders. Mirrors what UpstoxBroker._place_order actually accepts
    (see broker/upstox_broker.py) rather than an opaque dict, since a
    real broker order requires a concrete order_type/price/trigger_price,
    not caller-supplied free-form JSON.
    """
    instrument_token: str
    transaction_type: Literal["BUY", "SELL"]
    quantity: int
    order_type: Literal["MARKET", "LIMIT", "SL", "SL-M"] = "LIMIT"
    price: float = 0
    trigger_price: float = 0
    product: str = "D"


def validate_leg(broker, leg: dict) -> Optional[str]:
    """
    Creation-time sanity check, called from routes_advanced_orders.py
    before persisting/placing a leg — returns an error string (caller
    turns this into a 400) or None if OK. Catches the cheap, common
    mistakes (typo'd instrument_token, non-positive quantity, a LIMIT/SL
    order with no price/trigger set) immediately, rather than only ever
    discovering them when a live order gets rejected at the broker, or
    when a paper trigger silently never fires because it was 0 all along.
    Not exhaustive — margin/funds availability is still only checked at
    actual fill time (live: by the broker itself; paper: by
    PaperBroker._place_order), since that's a moving target this check
    can't usefully freeze at creation time.
    """
    if leg["quantity"] <= 0:
        return f"quantity must be positive, got {leg['quantity']}"
    if broker.get_ltp(leg["instrument_token"]) is None:
        return f"instrument_token '{leg['instrument_token']}' not recognized (no LTP available)"
    order_type = leg.get("order_type", "MARKET")
    if order_type == "LIMIT" and leg.get("price", 0) <= 0:
        return "price must be positive for a LIMIT order"
    if order_type in ("SL", "SL-M") and leg.get("trigger_price", 0) <= 0:
        return "trigger_price must be positive for an SL/SL-M order"
    if order_type == "SL" and leg.get("price", 0) <= 0:
        return "price must be positive for an SL (stop-limit) order"
    return None


def place_leg(broker, leg: dict, tag: str, user_id: Optional[int] = None) -> Optional[str]:
    """
    Place one leg (dict shape matching OrderLeg.dict()) at `broker`.
    Returns the broker order_id, or None (PaperBroker/UpstoxBroker
    dry-run both return None on non-placement; real failures raise).
    """
    fn = broker.place_sell_order if leg["transaction_type"] == "SELL" else broker.place_buy_order
    return fn(
        instrument_token=leg["instrument_token"],
        quantity=leg["quantity"],
        product=leg.get("product", "D"),
        order_type=leg.get("order_type", "MARKET"),
        tag=tag[:16],
        user_id=user_id,
        price=leg.get("price", 0),
        trigger_price=leg.get("trigger_price", 0),
    )


def leg_triggered(leg: dict, ltp: float) -> bool:
    """
    Paper-mode-only: has this leg's condition been met against the
    current LTP? UpstoxBroker orders never need this — a real LIMIT/SL
    order sits at the exchange and the exchange itself decides; this is
    only for simulating that same decision against live LTP for paper
    orders, which have no resting order of their own (see
    advanced_orders_scheduler.py module docstring).
    """
    order_type = leg.get("order_type", "MARKET")
    tt = leg["transaction_type"]
    if order_type == "MARKET":
        return True
    if order_type == "LIMIT":
        return ltp <= leg["price"] if tt == "BUY" else ltp >= leg["price"]
    if order_type in ("SL", "SL-M"):
        return ltp >= leg["trigger_price"] if tt == "BUY" else ltp <= leg["trigger_price"]
    return False


def simulate_paper_fill(broker, leg: dict, tag: str, user_id: Optional[int] = None) -> Optional[str]:
    """
    Paper-mode fill: leg_triggered() said the condition is met, so
    actually record the (virtual) fill via PaperBroker — always a MARKET
    call regardless of the leg's own order_type, since PaperBroker has no
    concept of resting LIMIT/SL orders; the trigger check above is what
    stands in for the exchange deciding "this would have filled now".
    """
    fn = broker.place_sell_order if leg["transaction_type"] == "SELL" else broker.place_buy_order
    return fn(
        instrument_token=leg["instrument_token"],
        quantity=leg["quantity"],
        product=leg.get("product", "D"),
        order_type="MARKET",
        tag=tag[:16],
        user_id=user_id,
    )
