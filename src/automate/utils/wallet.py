"""
utils/wallet.py — Derived virtual funds wallet for PAPER-mode trading.

Balance/margin/charges math is recomputed from the `positions` table (the
single existing source of truth — see position_tracker.py) on every
request — no separate balance row to drift out of sync. The one piece of
real mutable state is the starting capital itself (see WalletSettings,
db/migrations/versions/0002_wallet_settings.py, made per-user in migration
0015) — one row per user, editable at runtime via /api/wallet/capital,
rather than an env var that needs a process restart to change.

Every function here takes a real user_id and is scoped to that account's
own positions/wallet — each logged-in user has their own independent
virtual paper wallet, not one shared balance (see migration 0015's
docstring for why this changed from a single global row).

Live-mode funds are real money on the real broker's account — not simulated
here; this module only covers 'paper' positions.
"""
from typing import List, Optional

from automate.db.engine import get_session
from automate.db.models import WalletSettings
from automate.utils.margin import INDEX_SYMBOLS, estimate_margin_blocked, is_commodity_instrument_key
from automate.utils.pnl import compute_strangle_pnl, entry_charges_only
from automate.utils.position_tracker import get_closed_positions, get_open_positions
from automate.utils.wallet_adjustments import load_adjustments, total_adjustments


def get_starting_capital(user_id: int) -> float:
    with get_session() as session:
        row = session.query(WalletSettings).filter_by(user_id=user_id).first()
        return float(row.starting_capital) if row else 0.0


def set_starting_capital(user_id: int, value: float) -> float:
    if value < 0:
        raise ValueError("Starting capital can't be negative")

    # Reset/wipe out existing paper-trading history for THIS account only
    # (adjustments and closed positions) — resetting your own wallet must
    # never touch anyone else's.
    from automate.utils.wallet_adjustments import clear_adjustments
    from automate.utils.position_tracker import delete_closed_positions
    from automate.db.models import EquityPosition

    clear_adjustments(user_id)
    delete_closed_positions(mode="paper", user_id=user_id)

    with get_session() as session:
        # Wipe out this user's own virtual/paper equity positions (opened/closed)
        session.query(EquityPosition).where(
            (EquityPosition.mode == "paper") & (EquityPosition.user_id == user_id)
        ).delete(synchronize_session=False)

        row = session.query(WalletSettings).filter_by(user_id=user_id).first()
        if row is None:
            row = WalletSettings(user_id=user_id, starting_capital=value)
            session.add(row)
        else:
            row.starting_capital = value
    return value


def get_equity_positions(mode: str = "paper", status: str = None, user_id: Optional[int] = None) -> List[dict]:
    from automate.db.engine import get_session
    from automate.db.models import EquityPosition
    from sqlalchemy import select
    try:
        with get_session() as session:
            stmt = select(EquityPosition).where(EquityPosition.mode == mode)
            if status:
                stmt = stmt.where(EquityPosition.status == status)
            if user_id is not None:
                stmt = stmt.where(EquityPosition.user_id == user_id)
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


def _paper_broker():
    """
    The shared paper broker singleton, if the broker connection is ready
    — None otherwise (e.g. Upstox token not yet refreshed on this
    process's first tick). Used to prefer REAL Upstox-calculated margin
    for the wallet's margin_blocked figure, same policy order-time
    sizing/validation already use (utils/margin.py::resolve_required_margin)
    — callers here must fall back to the flat-rate estimate when this is
    None, same as everywhere else that calls it.
    """
    try:
        from automate.api.custom_strategy_scheduler import _get_brokers
        brokers = _get_brokers()
        return brokers["paper"] if brokers else None
    except Exception:
        return None


def _custom_strategy_wallet_stats(user_id: int, mode: str = "paper") -> dict:
    """
    Margin/charges/realized-P&L contribution from this user's OWN Custom
    Strategy Builder baskets — see routes_leaderboard.py for the same
    per-user basket-grouping approach.
    """
    from collections import defaultdict
    from automate.api.custom_strategy_scheduler import _is_leg_for_symbol
    from automate.db.engine import get_session
    from automate.db.models import CustomStrategy, CustomStrategyPosition
    from automate.utils.costs import calculate_options_transaction_cost_breakdown
    from automate.utils.pnl import compute_basket_pnl
    from sqlalchemy import select

    with get_session() as session:
        own_strategy_ids = set(session.execute(
            select(CustomStrategy.id).where(CustomStrategy.user_id == user_id)
        ).scalars().all())
        if not own_strategy_ids:
            return {"margin_blocked": 0.0, "entry_charges_open": 0.0, "realized_pnl": 0.0, "charges_closed": 0.0, "open_count": 0}

        legs = session.execute(
            select(CustomStrategyPosition).where(
                (CustomStrategyPosition.mode == mode) &
                (CustomStrategyPosition.strategy_id.in_(own_strategy_ids))
            )
        ).scalars().all()
        if not legs:
            return {"margin_blocked": 0.0, "entry_charges_open": 0.0, "realized_pnl": 0.0, "charges_closed": 0.0, "open_count": 0}

        strategies = {
            s.id: s for s in session.execute(
                select(CustomStrategy).where(CustomStrategy.id.in_({l.strategy_id for l in legs}))
            ).scalars().all()
        }

    # Basket = one entry cycle of one strategy+symbol (same grouping as
    # routes_leaderboard.py) — margin/realized-P&L must be computed per
    # basket, not per leg, so a strangle's two opposite-direction legs
    # don't each get charged full margin independently.
    open_baskets: dict = defaultdict(list)
    closed_baskets: dict = defaultdict(list)
    for leg in legs:
        strategy = strategies.get(leg.strategy_id)
        if strategy is None:
            continue
        import json
        symbols = json.loads(strategy.symbols)
        symbol = next((s for s in symbols if _is_leg_for_symbol(leg.instrument_key, s)), symbols[0] if symbols else "?")
        key = (leg.strategy_id, symbol, leg.opened_at.date() if leg.opened_at else None)
        (open_baskets if leg.status == "OPEN" else closed_baskets)[key].append((leg, symbol))

    margin_blocked = 0.0
    entry_charges_open = 0.0
    broker = _paper_broker()
    for basket_legs in open_baskets.values():
        legs_only = [l for l, _ in basket_legs]
        symbol = basket_legs[0][1]
        sell_legs = [l for l in legs_only if l.transaction_type == "SELL"]

        # Prefer the REAL Upstox-calculated NETTED margin for this whole
        # basket (a strangle's two SELL legs together need meaningfully
        # less than each summed independently) — falls back to the old
        # flat-rate estimate if the broker isn't ready or the call fails,
        # same tiered policy order-time sizing already uses (see
        # utils/margin.py::resolve_required_margin).
        real_margin = None
        if broker is not None and sell_legs:
            try:
                instruments = [
                    {"instrument_key": l.instrument_key, "quantity": l.quantity, "transaction_type": "SELL", "product": "D"}
                    for l in sell_legs
                ]
                real_margin = broker.get_basket_required_margin(instruments)
            except Exception:
                real_margin = None

        if real_margin is not None and real_margin > 0:
            margin_blocked += real_margin
        else:
            strikes = [float(l.strike) for l in legs_only if l.strike is not None]
            spot_proxy = sum(strikes) / len(strikes) if strikes else 0.0
            is_index = symbol.upper() in INDEX_SYMBOLS
            is_commodity = is_commodity_instrument_key(sell_legs[0].instrument_key) if sell_legs else False
            short_qty = max((l.quantity for l in sell_legs), default=0)
            if short_qty > 0 and spot_proxy > 0:
                margin_blocked += estimate_margin_blocked(spot_proxy, short_qty, is_index, is_commodity)

        for leg in legs_only:
            entry_charges_open += calculate_options_transaction_cost_breakdown(
                float(leg.entry_price), leg.quantity, leg.transaction_type
            )["total"]

    realized_pnl = 0.0
    charges_closed = 0.0
    for basket_legs in closed_baskets.values():
        legs_only = [l for l, _ in basket_legs]
        result = compute_basket_pnl([
            {"entry_price": l.entry_price, "exit_price": l.exit_price, "quantity": l.quantity, "transaction_type": l.transaction_type}
            for l in legs_only if l.exit_price is not None
        ])
        realized_pnl += result["net_pnl"]
        charges_closed += result["charges"]["total"]

    return {
        "margin_blocked": round(margin_blocked, 2),
        "entry_charges_open": round(entry_charges_open, 2),
        "realized_pnl": round(realized_pnl, 2),
        "charges_closed": round(charges_closed, 2),
        "open_count": len(open_baskets),
    }


def get_wallet_summary(user_id: int) -> dict:
    starting_capital = get_starting_capital(user_id)

    # 1. Options Positions
    open_paper = get_open_positions(mode="paper", user_id=user_id)
    closed_paper = get_closed_positions(limit=None, mode="paper", user_id=user_id)

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
    open_eq = get_equity_positions(mode="paper", status="OPEN", user_id=user_id)
    closed_eq = get_equity_positions(mode="paper", status="CLOSED", user_id=user_id)

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

    # 3. Custom Strategy Builder positions
    cs = _custom_strategy_wallet_stats(user_id, mode="paper")

    # 4. Totals
    total_charges_lifetime = round(
        entry_charges_open_opt + charges_closed_opt + entry_charges_open_eq + charges_closed_eq
        + cs["entry_charges_open"] + cs["charges_closed"],
        2,
    )
    adjustments_total = total_adjustments(user_id)

    available_balance = round(
        starting_capital + adjustments_total
        - margin_blocked_opt - entry_charges_open_opt
        - equity_invested_open - equity_margin_open - entry_charges_open_eq
        - cs["margin_blocked"] - cs["entry_charges_open"]
        + realized_pnl_opt + realized_pnl_eq + cs["realized_pnl"],
        2
    )

    return {
        "starting_capital": starting_capital,
        "available_balance": available_balance,
        "margin_blocked": round(margin_blocked_opt + equity_margin_open + cs["margin_blocked"], 2),
        "total_charges_lifetime": total_charges_lifetime,
        "realized_pnl_lifetime": round(realized_pnl_opt + realized_pnl_eq + cs["realized_pnl"], 2),
        "total_adjustments": adjustments_total,
        "open_positions_count": len(open_paper) + len(open_eq) + cs["open_count"],
    }


def get_ledger(user_id: int) -> List[dict]:
    """
    Chronological funds-statement rows (one OPEN + one CLOSE event per position,
    CLOSE omitted for still-open positions) for both options and equities,
    calculating a running balance walking forward from starting_capital.
    """
    starting_capital = get_starting_capital(user_id)

    # Fetch all data
    open_paper = get_open_positions(mode="paper", user_id=user_id)
    closed_paper = get_closed_positions(limit=None, mode="paper", user_id=user_id)
    open_eq = get_equity_positions(mode="paper", status="OPEN", user_id=user_id)
    closed_eq = get_equity_positions(mode="paper", status="CLOSED", user_id=user_id)

    events = []

    # 1. Manual Adjustments
    for adj in load_adjustments(user_id):
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
