"""
utils/wallet.py — Derived virtual funds wallet for PAPER-mode trading.

Balance/margin/charges math is recomputed from the `positions` table (the
single existing source of truth — see position_tracker.py) on every
request — no separate balance row to drift out of sync. The one piece of
real mutable state is the starting capital itself (see WalletSettings,
db/migrations/versions/0002_wallet_settings.py) — a single DB row, editable
at runtime via /api/wallet/capital, rather than an env var that needs a
process restart to change.

Live-mode funds are real money on the real broker's account — not simulated
here; this module only covers 'paper' positions.
"""
from typing import List

from automate.db.engine import get_session
from automate.db.models import WalletSettings
from automate.utils.margin import INDEX_SYMBOLS, estimate_margin_blocked
from automate.utils.pnl import compute_strangle_pnl, entry_charges_only
from automate.utils.position_tracker import get_closed_positions, get_open_positions
from automate.utils.wallet_adjustments import load_adjustments, total_adjustments

_SETTINGS_ROW_ID = 1


def get_starting_capital() -> float:
    with get_session() as session:
        row = session.get(WalletSettings, _SETTINGS_ROW_ID)
        return float(row.starting_capital) if row else 0.0


def set_starting_capital(value: float) -> float:
    if value < 0:
        raise ValueError("Starting capital can't be negative")

    # Reset/wipe out existing paper-trading history (adjustments and closed positions)
    from automate.utils.wallet_adjustments import clear_adjustments
    from automate.utils.position_tracker import delete_closed_positions
    from automate.db.models import EquityPosition

    clear_adjustments()
    delete_closed_positions(mode="paper")

    with get_session() as session:
        # Wipe out all virtual/paper equity positions (opened/closed)
        session.query(EquityPosition).where(EquityPosition.mode == "paper").delete(synchronize_session=False)

        row = session.get(WalletSettings, _SETTINGS_ROW_ID)
        if row is None:
            row = WalletSettings(id=_SETTINGS_ROW_ID, starting_capital=value)
            session.add(row)
        else:
            row.starting_capital = value
    return value



def get_equity_positions(mode: str = "paper", status: str = None) -> List[dict]:
    from automate.db.engine import get_session
    from automate.db.models import EquityPosition
    from sqlalchemy import select
    try:
        with get_session() as session:
            stmt = select(EquityPosition).where(EquityPosition.mode == mode)
            if status:
                stmt = stmt.where(EquityPosition.status == status)
            results = session.execute(stmt).scalars().all()
            return [r.to_dict() for r in results]
    except Exception:
        return []


def _margin_for(pos: dict) -> float:
    # No stored spot price on live positions — approximate it as the strike
    # midpoint, same "rough estimate" spirit as utils/margin.py itself.
    spot_proxy = (pos["call_strike"] + pos["put_strike"]) / 2
    is_index = pos["symbol"].upper() in INDEX_SYMBOLS
    return estimate_margin_blocked(spot_proxy, pos["quantity"], is_index)


def get_wallet_summary() -> dict:
    starting_capital = get_starting_capital()
    
    # 1. Options Positions
    open_paper = get_open_positions(mode="paper")
    closed_paper = get_closed_positions(limit=None, mode="paper")

    margin_blocked_opt = sum(_margin_for(p) for p in open_paper)
    entry_charges_open_opt = sum(
        entry_charges_only(p["call_entry_price"], p["put_entry_price"], p["quantity"])["total"]
        for p in open_paper
    )

    realized_pnl_opt = 0.0
    charges_closed_opt = 0.0
    for p in closed_paper:
        econ = compute_strangle_pnl(
            p["call_entry_price"], p["put_entry_price"],
            p["call_exit_price"], p["put_exit_price"], p["quantity"],
        )
        realized_pnl_opt += econ["net_pnl"]
        charges_closed_opt += econ["charges"]["total"]

    # 2. Equity / Manual Positions
    open_eq = get_equity_positions(mode="paper", status="OPEN")
    closed_eq = get_equity_positions(mode="paper", status="CLOSED")

    equity_invested_open = sum(
        float(p["entry_price"]) * p["quantity"]
        for p in open_eq if p["direction"] in ("BUY", "LONG")
    )
    equity_margin_open = sum(
        float(p["entry_price"]) * p["quantity"] * 0.1
        for p in open_eq if p["direction"] in ("SELL", "SHORT")
    )
    
    entry_charges_open_eq = sum(
        round((float(p["entry_price"]) * p["quantity"]) * 0.00015, 2)
        for p in open_eq
    )

    realized_pnl_eq = sum(float(p["net_pnl"] or 0) for p in closed_eq)
    charges_closed_eq = sum(float(p["charges"] or 0) for p in closed_eq)

    # 3. Totals
    total_charges_lifetime = round(
        entry_charges_open_opt + charges_closed_opt + entry_charges_open_eq + charges_closed_eq, 2
    )
    adjustments_total = total_adjustments()
    
    available_balance = round(
        starting_capital + adjustments_total 
        - margin_blocked_opt - entry_charges_open_opt
        - equity_invested_open - equity_margin_open - entry_charges_open_eq
        + realized_pnl_opt + realized_pnl_eq,
        2
    )

    return {
        "starting_capital": starting_capital,
        "available_balance": available_balance,
        "margin_blocked": round(margin_blocked_opt + equity_margin_open, 2),
        "total_charges_lifetime": total_charges_lifetime,
        "realized_pnl_lifetime": round(realized_pnl_opt + realized_pnl_eq, 2),
        "total_adjustments": adjustments_total,
        "open_positions_count": len(open_paper) + len(open_eq),
    }


def get_ledger() -> List[dict]:
    """
    Chronological funds-statement rows (one OPEN + one CLOSE event per position,
    CLOSE omitted for still-open positions) for both options and equities,
    calculating a running balance walking forward from starting_capital.
    """
    starting_capital = get_starting_capital()
    
    # Fetch all data
    open_paper = get_open_positions(mode="paper")
    closed_paper = get_closed_positions(limit=None, mode="paper")
    open_eq = get_equity_positions(mode="paper", status="OPEN")
    closed_eq = get_equity_positions(mode="paper", status="CLOSED")

    events = []

    # 1. Manual Adjustments
    for adj in load_adjustments():
        events.append({
            "date": adj["date"],
            "position_id": None,
            "type": "ADJUSTMENT",
            "description": adj["note"],
            "debit": abs(adj["amount"]) if adj["amount"] < 0 else 0.0,
            "credit": adj["amount"] if adj["amount"] >= 0 else 0.0,
            "_order": -1,
        })

    # 2. Options Open
    for p in open_paper:
        margin = _margin_for(p)
        charges = entry_charges_only(p["call_entry_price"], p["put_entry_price"], p["quantity"])
        events.append({
            "date": p["entry_date"],
            "position_id": p["id"],
            "type": "OPEN",
            "description": f"Sold {p['symbol']} {p['call_strike']}CE / {p['put_strike']}PE",
            "debit": round(margin + charges["total"], 2),
            "credit": 0.0,
            "_order": 0,
        })

    # 3. Options Closed
    for p in closed_paper:
        margin = _margin_for(p)
        econ = compute_strangle_pnl(
            p["call_entry_price"], p["put_entry_price"],
            p["call_exit_price"], p["put_exit_price"], p["quantity"],
        )
        entry_charges = entry_charges_only(p["call_entry_price"], p["put_entry_price"], p["quantity"])["total"]
        exit_charges = round(econ["charges"]["total"] - entry_charges, 2)
        
        events.append({
            "date": p["entry_date"],
            "position_id": p["id"],
            "type": "OPEN",
            "description": f"Sold {p['symbol']} {p['call_strike']}CE / {p['put_strike']}PE",
            "debit": round(margin + entry_charges, 2),
            "credit": 0.0,
            "_order": 0,
        })

        net_close_flow = round(margin + econ["gross_pnl"] - exit_charges, 2)
        events.append({
            "date": p["exit_date"],
            "position_id": p["id"],
            "type": "CLOSE",
            "description": f"Bought back {p['symbol']} {p['call_strike']}CE / {p['put_strike']}PE ({p['exit_reason']})",
            "debit": abs(net_close_flow) if net_close_flow < 0 else 0.0,
            "credit": net_close_flow if net_close_flow >= 0 else 0.0,
            "_order": 1,
        })

    # 4. Equities Open
    for p in open_eq:
        charges = round((float(p["entry_price"]) * p["quantity"]) * 0.00015, 2)
        if p["direction"] in ("BUY", "LONG"):
            debit_val = round((float(p["entry_price"]) * p["quantity"]) + charges, 2)
            desc = f"Bought {p['symbol']} Qty {p['quantity']} @ ₹{p['entry_price']}"
        else:
            margin = round(float(p["entry_price"]) * p["quantity"] * 0.1, 2)
            debit_val = round(margin + charges, 2)
            desc = f"Shorted {p['symbol']} Qty {p['quantity']} @ ₹{p['entry_price']}"
        events.append({
            "date": p["entry_date"],
            "position_id": p["id"],
            "type": "OPEN",
            "description": desc,
            "debit": debit_val,
            "credit": 0.0,
            "_order": 0,
        })

    # 5. Equities Closed
    for p in closed_eq:
        charges_val = float(p["charges"] or 0)
        entry_charges = round(charges_val / 2, 2)
        exit_charges = round(charges_val - entry_charges, 2)
        
        # Open entry
        if p["direction"] in ("BUY", "LONG"):
            debit_val = round((float(p["entry_price"]) * p["quantity"]) + entry_charges, 2)
            desc_open = f"Bought {p['symbol']} Qty {p['quantity']} @ ₹{p['entry_price']}"
        else:
            margin = round(float(p["entry_price"]) * p["quantity"] * 0.1, 2)
            debit_val = round(margin + entry_charges, 2)
            desc_open = f"Shorted {p['symbol']} Qty {p['quantity']} @ ₹{p['entry_price']}"
            
        events.append({
            "date": p["entry_date"],
            "position_id": p["id"],
            "type": "OPEN",
            "description": desc_open,
            "debit": debit_val,
            "credit": 0.0,
            "_order": 0,
        })
        
        # Close exit
        if p["direction"] in ("BUY", "LONG"):
            credit_val = round(float(p["exit_price"]) * p["quantity"], 2)
            debit_exit = exit_charges
            desc_close = f"Sold {p['symbol']} Qty {p['quantity']} @ ₹{p['exit_price']} ({p['exit_reason']})"
        else:
            margin = round(float(p["entry_price"]) * p["quantity"] * 0.1, 2)
            gross_pnl = float(p["gross_pnl"] or 0)
            net_close_flow = round(margin + gross_pnl - exit_charges, 2)
            credit_val = net_close_flow if net_close_flow >= 0 else 0.0
            debit_exit = abs(net_close_flow) if net_close_flow < 0 else 0.0
            desc_close = f"Covered {p['symbol']} Qty {p['quantity']} @ ₹{p['exit_price']} ({p['exit_reason']})"
            
        events.append({
            "date": p["exit_date"],
            "position_id": p["id"],
            "type": "CLOSE",
            "description": desc_close,
            "debit": debit_exit,
            "credit": credit_val,
            "_order": 1,
        })

    # Sort all events chronologically
    events.sort(key=lambda e: (e["date"], e["_order"]))

    balance = starting_capital
    ledger = []
    for e in events:
        balance = round(balance - e["debit"] + e["credit"], 2)
        ledger.append({
            "date": e["date"],
            "position_id": e["position_id"],
            "type": e["type"],
            "description": e["description"],
            "debit": e["debit"],
            "credit": e["credit"],
            "balance": balance,
        })
    return ledger

