"""
utils/orders.py — Derived order book.

Positions already store both legs' entry (and, once closed, exit) fill
prices and order IDs — this expands each position row into up to 4 synthetic
order rows (CE/PE entry SELL, CE/PE exit BUY) instead of keeping a separate
leg-level order log, matching the "single source of truth" approach the
paper broker's own docstring already commits to.
"""

from automate.utils.costs import calculate_options_transaction_cost_breakdown
from automate.utils.position_tracker import get_closed_positions, get_open_positions


def _leg_order(pos: dict, leg: str, side: str, strike: int, price: float, order_id: str | None, order_date: str, rates: dict | None = None) -> dict:
    charges = calculate_options_transaction_cost_breakdown(price, pos["quantity"], side, rates)
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


def _custom_strategy_orders(user_id: int | None, mode: str | None, rates: dict | None = None) -> list[dict]:
    """
    Synthetic order rows for Custom Strategy Builder legs (CustomStrategyPosition)
    — same derivation approach as the option-strangle section above (positions
    are the source of truth, orders are read off them), since custom strategies
    never got their own separate order log either. Unlike the legacy sections
    below, this one IS scoped to user_id (every CustomStrategy row has a real
    owner — see the per-user isolation work elsewhere in this API — so leaving
    it unscoped here would leak other users' custom-strategy fills into this
    account's Orders page).
    """
    if user_id is None:
        return []
    import json

    from sqlalchemy import select

    from automate.api.custom_strategy_scheduler import _is_leg_for_symbol
    from automate.db.engine import get_session
    from automate.db.models import CustomStrategy, CustomStrategyPosition

    orders: list[dict] = []
    with get_session() as session:
        own_ids = set(session.execute(
            select(CustomStrategy.id).where(CustomStrategy.user_id == user_id)
        ).scalars().all())
        if not own_ids:
            return []

        stmt = select(CustomStrategyPosition).where(CustomStrategyPosition.strategy_id.in_(own_ids))
        if mode:
            stmt = stmt.where(CustomStrategyPosition.mode == mode)
        legs = session.execute(stmt).scalars().all()
        if not legs:
            return []

        strategies = {
            s.id: s for s in session.execute(
                select(CustomStrategy).where(CustomStrategy.id.in_({leg.strategy_id for leg in legs}))
            ).scalars().all()
        }

    for leg in legs:
        strategy = strategies.get(leg.strategy_id)
        strategy_name = strategy.name if strategy else "Custom Strategy"
        underlying_symbols = json.loads(strategy.symbols) if strategy else []
        underlying = next((s for s in underlying_symbols if _is_leg_for_symbol(leg.instrument_key, s)), underlying_symbols[0] if underlying_symbols else leg.instrument_key)
        display = f"{underlying} {leg.strike} {leg.option_type}" if leg.strike is not None else underlying

        entry_price = float(leg.entry_price)
        # Full timestamp (not just the date) — OrdersView.tsx's Orders page
        # splits a time-of-day out of this field to show in its "Time"
        # column, matching the legacy strangle rows' entry_date format.
        entry_date = leg.opened_at.isoformat() if leg.opened_at else ""
        entry_charges = calculate_options_transaction_cost_breakdown(entry_price, leg.quantity, leg.transaction_type, rates)
        orders.append({
            "order_id": leg.order_id,
            "date": entry_date,
            "position_id": leg.strategy_id,
            "strategy_name": strategy_name,
            "mode": leg.mode,
            "symbol": underlying,
            "display_symbol": display,
            "leg": leg.option_type or leg.instrument_type,
            "strike": leg.strike,
            "transaction_type": leg.transaction_type,
            "quantity": leg.quantity,
            "price": entry_price,
            "charges": entry_charges["total"],
            "status": "COMPLETE",
        })
        if leg.status == "CLOSED" and leg.exit_price is not None:
            exit_side = "BUY" if leg.transaction_type == "SELL" else "SELL"
            exit_price = float(leg.exit_price)
            exit_date = leg.closed_at.isoformat() if leg.closed_at else entry_date
            exit_charges = calculate_options_transaction_cost_breakdown(exit_price, leg.quantity, exit_side, rates)
            orders.append({
                "order_id": leg.exit_order_id,
                "date": exit_date,
                "position_id": leg.strategy_id,
                "strategy_name": strategy_name,
                "mode": leg.mode,
                "symbol": underlying,
                "display_symbol": display,
                "leg": leg.option_type or leg.instrument_type,
                "strike": leg.strike,
                "transaction_type": exit_side,
                "quantity": leg.quantity,
                "price": exit_price,
                "charges": exit_charges["total"],
                "status": "COMPLETE",
            })
    return orders


def get_order_book(mode: str | None = None, limit: int = 200, user_id: int | None = None) -> list[dict]:
    rates = None
    if user_id is not None:
        from automate.utils.wallet import get_charge_rates
        rates = get_charge_rates(user_id)

    # 1. Option strangles
    positions = get_open_positions(mode=mode, user_id=user_id) + get_closed_positions(limit=None, mode=mode, user_id=user_id)
    orders: list[dict] = []

    for p in positions:
        orders.append(_leg_order(p, "CE", "SELL", p["call_strike"], p["call_entry_price"], p["call_order_id"], p["entry_date"], rates))
        orders.append(_leg_order(p, "PE", "SELL", p["put_strike"], p["put_entry_price"], p["put_order_id"], p["entry_date"], rates))
        if p["status"] == "CLOSED":
            orders.append(_leg_order(p, "CE", "BUY", p["call_strike"], p["call_exit_price"], p["call_exit_order_id"], p["exit_date"], rates))
            orders.append(_leg_order(p, "PE", "BUY", p["put_strike"], p["put_exit_price"], p["put_exit_order_id"], p["exit_date"], rates))

    # 1b. Custom Strategy Builder legs
    orders.extend(_custom_strategy_orders(user_id, mode, rates))

    # 2. Equity and manual terminal positions
    from sqlalchemy import select

    from automate.db.engine import get_session
    from automate.db.models import EquityPosition
    
    equity_positions = []
    try:
        with get_session() as session:
            stmt = select(EquityPosition)
            if mode:
                stmt = stmt.where(EquityPosition.mode == mode)
            if user_id is not None:
                stmt = stmt.where(EquityPosition.user_id == user_id)
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

