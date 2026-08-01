"""
utils/trailing_stop.py — shared trailing-stop ratchet math.

Used by both api/advanced_orders_scheduler.py (standalone trailing-stop
orders) and api/custom_strategy_scheduler.py (per-leg trailing stops on a
custom strategy — see rule_schema.py's leg.exit.trailing). Extracted here
(originally lived inline in advanced_orders_scheduler.py) so both callers
run the exact same tested math instead of two copies silently drifting
apart over time. Pure functions — no broker/DB access, easy to unit test
exhaustively.
"""
from typing import Optional, Tuple


def advance_trailing_stop(
    side: str,
    ltp: float,
    trail_amount: float,
    trail_type: str,
    highest_price: Optional[float],
    lowest_price: Optional[float],
    current_stop_price: Optional[float],
) -> Tuple[Optional[float], Optional[float], Optional[float], bool]:
    """
    Recompute a trailing stop's ratchet state for the latest LTP.

    side="BUY" (protecting a LONG position): the stop trails UPWARD just
    under the price as it rises, exits via SELL when price falls back to
    it. side="SELL" (protecting a SHORT position): the stop trails
    DOWNWARD just above the price as it falls, exits via BUY when price
    rises back to it. A stop only ever tightens, never loosens — it's
    updated only when a NEW favorable extreme (highest for BUY, lowest
    for SELL) is reached, otherwise every value is returned unchanged.

    Returns (new_highest_price, new_lowest_price, new_current_stop_price,
    advanced) — advanced=True iff the stop actually moved this call, the
    signal callers use to decide whether a live resting order needs a
    modify_order() push.
    """
    advanced = False
    if side == "BUY":
        if highest_price is None or ltp > highest_price:
            highest_price = ltp
            current_stop_price = ltp - trail_amount if trail_type == "points" else ltp * (1 - trail_amount / 100)
            advanced = True
    else:
        if lowest_price is None or ltp < lowest_price:
            lowest_price = ltp
            current_stop_price = ltp + trail_amount if trail_type == "points" else ltp * (1 + trail_amount / 100)
            advanced = True
    return highest_price, lowest_price, current_stop_price, advanced


def exit_transaction_type(side: str) -> str:
    """The direction an exit order for this trailing stop must transact — opposite of the protected position's own side."""
    return "SELL" if side == "BUY" else "BUY"


def stop_triggered(side: str, ltp: float, current_stop_price: Optional[float]) -> bool:
    """Has price crossed back through the current trailing-stop level? side=BUY (long) -> ltp <= stop; side=SELL (short) -> ltp >= stop."""
    if current_stop_price is None:
        return False
    return ltp <= current_stop_price if side == "BUY" else ltp >= current_stop_price
