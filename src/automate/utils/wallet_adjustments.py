"""
utils/wallet_adjustments.py — Manual deposit/withdrawal log for the virtual
paper-trading wallet.

Same pattern as utils/strategy_overrides.py: a small JSON file under
data/runtime/, not a DB table — this is control-panel-only state (a person
topping up or trimming their own virtual balance), not trade data, so it
doesn't belong in `positions`. Each entry is its own funds-statement line
(see utils/wallet.py::get_ledger()) rather than mutating a single number, so
the record of *when* and *why* the balance changed is never lost.

One file PER USER (wallet_adjustments_{user_id}.json) — each account's own
virtual wallet, own deposit/withdrawal history, isolated from everyone
else's, same as the wallet's own starting_capital (see WalletSettings,
now per-user via migration 0015).
"""
import fcntl
import json
from datetime import date
from pathlib import Path
from typing import List

_ADJUSTMENTS_DIR = "data/runtime"


def _path_for(user_id: int) -> str:
    return f"{_ADJUSTMENTS_DIR}/wallet_adjustments_{user_id}.json"


def load_adjustments(user_id: int) -> List[dict]:
    p = Path(_path_for(user_id))
    if not p.exists():
        return []
    with p.open() as f:
        return json.load(f)


def add_adjustment(user_id: int, amount: float, note: str = "") -> dict:
    if amount == 0:
        raise ValueError("Adjustment amount must be non-zero")

    p = Path(_path_for(user_id))
    p.parent.mkdir(parents=True, exist_ok=True)

    # flock the whole read-modify-write critical section — without this,
    # two concurrent requests for the SAME user (double-click submit, two
    # browser tabs) can both read the same base list before either writes,
    # and the second write silently clobbers the first adjustment (lost
    # update). "a+" both creates the file if missing and leaves existing
    # content in place for the read below (unlike "w", which would
    # truncate before we've read anything).
    with open(p, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.seek(0)
            raw = f.read()
            adjustments = json.loads(raw) if raw.strip() else []

            entry = {
                "id": (max((a["id"] for a in adjustments), default=0) + 1),
                "date": date.today().isoformat(),
                "amount": round(amount, 2),
                "note": note.strip() or ("Deposit" if amount > 0 else "Withdrawal"),
            }
            adjustments.append(entry)

            f.seek(0)
            f.truncate()
            f.write(json.dumps(adjustments, indent=2))
            return entry
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def total_adjustments(user_id: int) -> float:
    return round(sum(a["amount"] for a in load_adjustments(user_id)), 2)


def clear_adjustments(user_id: int) -> None:
    p = Path(_path_for(user_id))
    if p.exists():
        p.unlink()
