"""
utils/position_reconciliation.py — detects (never auto-corrects) drift
between this app's own DB records and the LIVE broker's real open
positions.

Why this exists: custom_strategy_scheduler.py places a real broker order
FIRST, then writes/commits the corresponding CustomStrategyPosition row
(see _try_entry/_try_exit) — there is no way to make "place a real order"
and "commit a local DB row" a single atomic operation across two
different systems. If the process dies in that window (crash, OOM kill,
bad deploy), the broker ends up holding a real position this app has no
record of, and on restart the scheduler — seeing nothing open — will
happily try to enter the SAME cycle again, doubling the real position.
The mirror case on exit (order placed, DB commit lost) leaves a leg
marked OPEN that's actually already closed at the broker.

This module is the safety net for exactly that: compare DB-expected net
quantity per instrument against the broker's actual net quantity and
report every mismatch. It deliberately never guesses which side is
"right" and fixes it automatically — a wrong auto-correction on a live
account is a worse outcome than a detected-but-unresolved drift a human
reviews. See api/custom_strategy_scheduler.py for where this gets called
on a schedule and alerted via notify().

PAPER mode is intentionally out of scope — there's no real exchange to
reconcile against; the DB already IS the sole source of truth there.
"""
from collections import defaultdict
from typing import TypedDict


class PositionMismatch(TypedDict):
    instrument_key: str
    db_quantity: int
    broker_quantity: int


def compute_db_net_quantity(open_live_legs: list[dict]) -> dict[str, int]:
    """
    Net expected quantity per instrument_key from this app's own OPEN,
    mode='live' CustomStrategyPosition rows — positive = net long
    (BUY), negative = net short (SELL), matching Upstox's own sign
    convention for get_positions(). Takes plain dicts (not ORM objects)
    — {instrument_key, transaction_type, quantity} — so this stays a
    pure, trivially-testable function; the caller does the DB query.
    """
    net: dict[str, int] = defaultdict(int)
    for leg in open_live_legs:
        sign = 1 if leg["transaction_type"] == "BUY" else -1
        net[leg["instrument_key"]] += sign * leg["quantity"]
    return dict(net)


def diff_positions(db_net: dict[str, int], broker_net: dict[str, int]) -> list[PositionMismatch]:
    """
    Every instrument where the DB's expected net quantity and the
    broker's actual net quantity disagree — in EITHER direction: an
    orphaned broker position the DB never recorded (the crash-after-order
    scenario), or a DB row still marked open that the broker shows flat
    (closed outside the app, or the crash-after-exit scenario). An
    instrument absent from one side is treated as 0 for that side.
    """
    mismatches: list[PositionMismatch] = []
    for instrument_key in sorted(set(db_net) | set(broker_net)):
        db_qty = db_net.get(instrument_key, 0)
        broker_qty = broker_net.get(instrument_key, 0)
        if db_qty != broker_qty:
            mismatches.append({"instrument_key": instrument_key, "db_quantity": db_qty, "broker_quantity": broker_qty})
    return mismatches


def reconcile_live_positions(open_live_legs: list[dict], broker_net: dict[str, int] | None) -> list[PositionMismatch] | None:
    """
    The one function callers should use — combines compute_db_net_quantity
    + diff_positions, and handles broker_net being unavailable (the
    broker call itself failed, e.g. a network hiccup) by returning None
    ("couldn't check this time") rather than a false "everything matches."
    """
    if broker_net is None:
        return None
    return diff_positions(compute_db_net_quantity(open_live_legs), broker_net)
