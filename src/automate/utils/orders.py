"""
utils/orders.py — Derived order book.

Positions already store both legs' entry (and, once closed, exit) fill
prices and order IDs — this expands each position row into up to 4 synthetic
order rows (CE/PE entry SELL, CE/PE exit BUY) instead of keeping a separate
leg-level order log, matching the "single source of truth" approach the
paper broker's own docstring already commits to.
"""
from typing import List, Optional

from automate.utils.costs import calculate_options_transaction_cost_breakdown
from automate.utils.position_tracker import get_closed_positions, get_open_positions


def _leg_order(pos: dict, leg: str, side: str, strike: int, price: float, order_id: Optional[str], order_date: str) -> dict:
    charges = calculate_options_transaction_cost_breakdown(price, pos["quantity"], side)
    return {
        "order_id": order_id,
        "date": order_date,
        "position_id": pos["id"],
        "strategy_name": pos["strategy_name"],
        "mode": pos["mode"],
        "symbol": pos["symbol"],
        "leg": leg,
        "strike": strike,
        "transaction_type": side,
        "quantity": pos["quantity"],
        "price": price,
        "charges": charges["total"],
        "status": "COMPLETE",
    }


def get_order_book(mode: Optional[str] = None, limit: int = 200) -> List[dict]:
    # 1. Option strangles
    positions = get_open_positions(mode=mode) + get_closed_positions(limit=None, mode=mode)
    orders: List[dict] = []
    
    for p in positions:
        orders.append(_leg_order(p, "CE", "SELL", p["call_strike"], p["call_entry_price"], p["call_order_id"], p["entry_date"]))
        orders.append(_leg_order(p, "PE", "SELL", p["put_strike"], p["put_entry_price"], p["put_order_id"], p["entry_date"]))
        if p["status"] == "CLOSED":
            orders.append(_leg_order(p, "CE", "BUY", p["call_strike"], p["call_exit_price"], p["call_exit_order_id"], p["exit_date"]))
            orders.append(_leg_order(p, "PE", "BUY", p["put_strike"], p["put_exit_price"], p["put_exit_order_id"], p["exit_date"]))

    # 2. Equity and manual terminal positions
    from automate.db.engine import get_session
    from automate.db.models import EquityPosition
    from sqlalchemy import select
    
    equity_positions = []
    try:
        with get_session() as session:
            stmt = select(EquityPosition)
            if mode:
                stmt = stmt.where(EquityPosition.mode == mode)
            results = session.execute(stmt).scalars().all()
            equity_positions = [r.to_dict() for r in results]
    except Exception:
        pass

    from automate.utils.instrument_display import resolve_display_symbols
    display_symbols = resolve_display_symbols(ep["symbol"] for ep in equity_positions)

    for ep in equity_positions:
        display_symbol = display_symbols.get(ep["symbol"], ep["symbol"])
        charges_total = float(ep["charges"] or 0)
        if ep["status"] == "CLOSED":
            entry_charges = round(charges_total / 2, 2)
            exit_charges = round(charges_total - entry_charges, 2)
        else:
            entry_charges = round((float(ep["entry_price"]) * ep["quantity"]) * 0.00015, 2)
            exit_charges = 0.0

        entry_side = "BUY" if ep["direction"] in ("LONG", "BUY") else "SELL"
        orders.append({
            "order_id": ep["entry_order_id"],
            "date": ep["entry_date"],
            "position_id": ep["id"],
            "strategy_name": ep["strategy_name"],
            "mode": ep["mode"],
            "symbol": ep["symbol"],
            "display_symbol": display_symbol,
            "leg": ep["product"],
            "strike": None,
            "transaction_type": entry_side,
            "quantity": ep["quantity"],
            "price": float(ep["entry_price"]),
            "charges": entry_charges,
            "status": "COMPLETE",
        })

        if ep["status"] == "CLOSED":
            exit_side = "SELL" if entry_side == "BUY" else "BUY"
            orders.append({
                "order_id": ep["exit_order_id"],
                "date": ep["exit_date"],
                "position_id": ep["id"],
                "strategy_name": ep["strategy_name"],
                "mode": ep["mode"],
                "symbol": ep["symbol"],
                "display_symbol": display_symbol,
                "leg": ep["product"],
                "strike": None,
                "transaction_type": exit_side,
                "quantity": ep["quantity"],
                "price": float(ep["exit_price"]) if ep["exit_price"] else 0.0,
                "charges": exit_charges,
                "status": "COMPLETE",
            })

    orders.sort(key=lambda o: (o["date"], o["position_id"] or 0), reverse=True)
    return orders[:limit]

