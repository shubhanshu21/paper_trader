"""
api/advanced_orders_scheduler.py — background asyncio task (started from
main.py's startup hook, same pattern as custom_strategy_scheduler.py — no
second systemd service) that drives every ACTIVE db.models.AdvancedOrder
row created via routes_advanced_orders.py.

Two fundamentally different execution models, by mode:

  - mode='live': a leg becomes a REAL resting order at the broker the
    moment it's placeable (OCO/bracket-TP-SL legs at creation or once a
    bracket's entry fills; a trailing stop's SL-M order as soon as the
    first tick has an LTP to anchor to). This task's job is then just to
    POLL broker.get_order_status() for fills, cancel the sibling leg of
    an OCO pair once one side completes, and push modify_order() calls to
    advance a trailing stop's trigger_price.

  - mode='paper': PaperBroker has no resting-order concept at all (see
    broker/paper_broker.py — place_*_order always fills immediately at
    current LTP). So a paper LIMIT/SL/trailing-stop leg is never sent to
    the broker while PENDING; every tick, this task itself evaluates
    whether the leg's condition would have been met against the current
    LTP (advanced_orders_common.leg_triggered) and, if so, calls
    PaperBroker to record the (virtual) fill right then. This is the
    direct paper-mode analogue of what a real exchange's matching engine
    does continuously for a live resting order.

Runs at a tighter interval than custom_strategy_scheduler.py
(_TICK_SEC vs that module's 60s/300s) because a stop that isn't advanced
or a fill that isn't detected for a full minute is a materially worse
outcome here than for strategy entry/exit timing.
"""
import asyncio
import json
from typing import Optional

from automate.compliance.sebi_rules import assert_market_is_open
from automate.db.engine import SessionLocal
from automate.db.models import AdvancedOrder
from automate.utils.logger import get_logger
from automate.utils.notify import notify
from automate.utils.trailing_stop import advance_trailing_stop, exit_transaction_type
from automate.api.advanced_orders_common import leg_triggered, place_leg, simulate_paper_fill

log = get_logger(__name__)

_TICK_SEC = 15


def _market_is_open_now() -> bool:
    try:
        assert_market_is_open()
        return True
    except RuntimeError:
        return False


def _get_brokers():
    from automate.api.custom_strategy_scheduler import _get_brokers as _shared_get_brokers
    return _shared_get_brokers()


def _cancel_leg(broker, leg: dict) -> None:
    if leg.get("status") == "PLACED" and leg.get("order_id"):
        broker.cancel_order(leg["order_id"])
    leg["status"] = "CANCELLED"


def _tick_oco_pair(order: AdvancedOrder, brokers: dict, state: dict, primary_key: str, secondary_key: str) -> bool:
    """
    Shared OCO-pair advance logic for both the OCO kind and a bracket's
    take-profit/stop-loss pair once its entry has filled. Mutates `state`
    in place. Returns True if the order is now COMPLETE/settled.
    """
    broker = brokers[order.mode]
    primary = state[primary_key]
    secondary = state[secondary_key]

    if order.mode == "live":
        for leg in (primary, secondary):
            if leg["status"] != "PLACED" or not leg.get("order_id"):
                continue
            leg_status = broker.get_order_status(leg["order_id"])
            if leg_status == "complete":
                leg["status"] = "COMPLETE"
            elif leg_status in ("cancelled", "rejected"):
                leg["status"] = leg_status.upper()

        if primary["status"] == "COMPLETE" or secondary["status"] == "COMPLETE":
            other = secondary if primary["status"] == "COMPLETE" else primary
            _cancel_leg(broker, other)
            return True
        if primary["status"] in ("CANCELLED", "REJECTED") and secondary["status"] in ("CANCELLED", "REJECTED"):
            return True
        return False

    # paper mode: nothing is resting at a real exchange — evaluate both
    # legs' trigger conditions against current LTP ourselves.
    for leg, other in ((primary, secondary), (secondary, primary)):
        if leg["status"] != "PENDING":
            continue
        ltp = broker.get_ltp(leg["instrument_token"])
        if ltp is None:
            continue
        if leg_triggered(leg, ltp):
            try:
                leg["order_id"] = simulate_paper_fill(broker, leg, f"{order.kind}_{order.public_id}"[:16], order.user_id)
                leg["status"] = "COMPLETE"
                leg["fill_price"] = ltp
            except Exception as exc:
                log.error("advanced_orders_scheduler: paper fill failed for %s leg of %s: %s", primary_key, order.public_id, exc)
                continue
            other["status"] = "CANCELLED"
            return True
    return False


def _tick_oco(order: AdvancedOrder, brokers: dict) -> None:
    state = json.loads(order.state_json)
    if _tick_oco_pair(order, brokers, state, "primary_order", "secondary_order"):
        order.status = "COMPLETED"
    order.state_json = json.dumps(state)


def _tick_trailing_stop(order: AdvancedOrder, brokers: dict) -> None:
    broker = brokers[order.mode]
    state = json.loads(order.state_json)
    ltp = broker.get_ltp(state["instrument_token"])
    if ltp is None:
        return

    # Live: a fill already happened at the broker — nothing left to trail.
    if order.mode == "live" and state.get("broker_order_id"):
        leg_status = broker.get_order_status(state["broker_order_id"])
        if leg_status == "complete":
            state["exit_order_id"] = state["broker_order_id"]
            state["exit_price"] = ltp
            order.status = "COMPLETED"
            order.state_json = json.dumps(state)
            return
        if leg_status in ("cancelled", "rejected"):
            order.status = "CANCELLED"
            order.state_json = json.dumps(state)
            return

    side = state["side"]
    state["highest_price"], state["lowest_price"], state["current_stop_price"], advanced = advance_trailing_stop(
        side, ltp, state["trail_amount"], state["trail_type"],
        state["highest_price"], state["lowest_price"], state["current_stop_price"],
    )

    exit_leg = {
        "instrument_token": state["instrument_token"],
        "transaction_type": exit_transaction_type(side),
        "quantity": state["quantity"],
        "order_type": "SL-M",
        "trigger_price": state["current_stop_price"],
        "product": state.get("product", "D"),
    }

    if order.mode == "live":
        if state.get("broker_order_id") is None:
            try:
                state["broker_order_id"] = place_leg(broker, exit_leg, f"TS_{order.public_id}", order.user_id)
            except Exception as exc:
                log.error("advanced_orders_scheduler: failed to place initial trailing-stop order for %s: %s", order.public_id, exc)
        elif advanced:
            ok = broker.modify_order(
                state["broker_order_id"], order_type="SL-M", trigger_price=state["current_stop_price"],
                quantity=state["quantity"],
            )
            if not ok:
                state["modify_failures"] = state.get("modify_failures", 0) + 1
                log.warning(
                    "advanced_orders_scheduler: failed to advance trailing-stop trigger for %s (%d consecutive failure(s)) — "
                    "the resting broker order is now stale at an older trigger price.",
                    order.public_id, state["modify_failures"],
                )
                # Alert once per stale streak (not every tick) — a single
                # rejected modify is common (e.g. a momentary broker
                # hiccup) and self-heals next tick; a repeated failure
                # means the real stop resting at the exchange is
                # meaningfully behind where the user expects it.
                if state["modify_failures"] == 3:
                    notify(
                        "advanced_orders",
                        f"Trailing stop {order.public_id} ({state['symbol']}) failed to advance its trigger price "
                        f"{state['modify_failures']} ticks in a row — the order resting at the broker is now stale "
                        f"at an older stop level than intended. Check it manually.",
                        level="warning",
                        user_id=order.user_id,
                    )
            else:
                state["modify_failures"] = 0
    else:
        # Paper: exchange-side matching is simulated by us — check whether
        # the just-recomputed stop has already been crossed this same tick.
        if leg_triggered({**exit_leg, "order_type": "SL-M"}, ltp):
            try:
                exit_order_id = simulate_paper_fill(broker, exit_leg, f"TS_{order.public_id}", order.user_id)
            except Exception as exc:
                # Leave ACTIVE and retry next tick rather than marking
                # COMPLETED with no real fill — e.g. a paper wallet-balance
                # rejection inside PaperBroker._place_order shouldn't be
                # reported as a successful exit.
                log.error("advanced_orders_scheduler: paper trailing-stop fill failed for %s: %s", order.public_id, exc)
            else:
                state["exit_order_id"] = exit_order_id
                state["exit_price"] = ltp
                order.status = "COMPLETED"

    order.state_json = json.dumps(state)


def _tick_bracket(order: AdvancedOrder, brokers: dict) -> None:
    broker = brokers[order.mode]
    state = json.loads(order.state_json)
    entry = state["entry_order"]

    if entry["status"] == "PENDING":
        # Should never happen — routes_advanced_orders.py always places
        # (live) or fills (paper) the entry synchronously at creation.
        return

    if entry["status"] == "PLACED":
        # Live only — poll until the entry itself fills before the
        # take-profit/stop-loss pair can be placed.
        leg_status = broker.get_order_status(entry["order_id"])
        if leg_status == "complete":
            entry["status"] = "COMPLETE"
        elif leg_status in ("cancelled", "rejected"):
            entry["status"] = leg_status.upper()
            order.status = "CANCELLED"
            order.state_json = json.dumps(state)
            return
        else:
            order.state_json = json.dumps(state)
            return

    # Entry filled — place (live) or arm (paper) the TP/SL OCO pair.
    if state["take_profit"]["status"] == "PENDING" and order.mode == "live" and not state["take_profit"].get("order_id"):
        try:
            state["take_profit"]["order_id"] = place_leg(broker, state["take_profit"], f"BRKTP_{order.public_id}", order.user_id)
            state["take_profit"]["status"] = "PLACED"
            state["stop_loss"]["order_id"] = place_leg(broker, state["stop_loss"], f"BRKSL_{order.public_id}", order.user_id)
            state["stop_loss"]["status"] = "PLACED"
        except Exception as exc:
            log.critical("advanced_orders_scheduler: failed to arm bracket TP/SL for %s: %s — MANUAL INTERVENTION REQUIRED.", order.public_id, exc)
            notify(
                "advanced_orders",
                f"Bracket order {order.public_id}'s entry filled but its take-profit/stop-loss pair failed to place: {exc}. "
                f"The position is currently unprotected — please place a manual exit.",
                user_id=order.user_id,
            )
            order.state_json = json.dumps(state)
            return

    if _tick_oco_pair(order, brokers, state, "take_profit", "stop_loss"):
        order.status = "COMPLETED"
    order.state_json = json.dumps(state)


async def advanced_orders_scheduler() -> None:
    """Persistent background task — see module docstring. Never raises; logs and keeps ticking."""
    log.info("advanced_orders_scheduler: started.")
    while True:
        try:
            if _market_is_open_now():
                brokers = _get_brokers()
                if brokers is not None:
                    db = SessionLocal()
                    try:
                        orders = db.query(AdvancedOrder).filter(AdvancedOrder.status == "ACTIVE").all()
                        for order in orders:
                            try:
                                if order.kind == "OCO":
                                    _tick_oco(order, brokers)
                                elif order.kind == "TRAILING_STOP":
                                    _tick_trailing_stop(order, brokers)
                                elif order.kind == "BRACKET":
                                    _tick_bracket(order, brokers)
                            except Exception as exc:
                                log.error("advanced_orders_scheduler: tick failed for %s %s: %s", order.kind, order.public_id, exc, exc_info=True)
                        db.commit()
                    finally:
                        db.close()
        except Exception as exc:
            log.error("advanced_orders_scheduler: tick-level failure: %s", exc, exc_info=True)

        await asyncio.sleep(_TICK_SEC)
