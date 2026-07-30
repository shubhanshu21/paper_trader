"""
utils/wallet_adjustments.py — Manual deposit/withdrawal log for the virtual
paper-trading wallet.

Same pattern as utils/strategy_overrides.py: a small JSON file under
data/runtime/, not a DB table — this is control-panel-only state (a person
topping up or trimming their own virtual balance), not trade data, so it
doesn't belong in `positions`. Each entry is its own funds-statement line
(see utils/wallet.py::get_ledger()) rather than mutating a single number, so
the record of *when* and *why* the balance changed is never lost.
"""
import json
from datetime import date
from pathlib import Path
from typing import List

ADJUSTMENTS_PATH = "data/runtime/wallet_adjustments.json"


def load_adjustments(path: str = ADJUSTMENTS_PATH) -> List[dict]:
    p = Path(path)
    if not p.exists():
        return []
    with p.open() as f:
        return json.load(f)


def add_adjustment(amount: float, note: str = "", path: str = ADJUSTMENTS_PATH) -> dict:
    if amount == 0:
        raise ValueError("Adjustment amount must be non-zero")

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    adjustments = load_adjustments(path)
    entry = {
        "id": (max((a["id"] for a in adjustments), default=0) + 1),
        "date": date.today().isoformat(),
        "amount": round(amount, 2),
        "note": note.strip() or ("Deposit" if amount > 0 else "Withdrawal"),
    }
    adjustments.append(entry)
    p.write_text(json.dumps(adjustments, indent=2))
    return entry


def total_adjustments(path: str = ADJUSTMENTS_PATH) -> float:
    return round(sum(a["amount"] for a in load_adjustments(path)), 2)


def clear_adjustments(path: str = ADJUSTMENTS_PATH) -> None:
    p = Path(path)
    if p.exists():
        p.unlink()
