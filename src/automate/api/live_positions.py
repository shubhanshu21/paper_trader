"""
api/live_positions.py — shared computation for a user's open custom-
strategy positions with live LTP/P&L/Chg%, used by both the WebSocket
push (ws_custom_strategy_positions.py — what the frontend Positions page
actually uses) and the one-off REST endpoint (routes_custom_strategies.py's
GET .../positions/open, kept for manual/curl inspection) so the logic
exists in exactly one place — same pattern as api/live_greeks.py.
"""
import json
from typing import Dict, List

from automate.db.engine import SessionLocal
from automate.db.models import CustomStrategy, CustomStrategyPosition


def compute_open_positions(user_id: int) -> List[dict]:
    """Every OPEN custom-strategy leg across all of `user_id`'s strategies, as flat per-leg rows."""
    from automate.api.custom_strategy_scheduler import _get_brokers, _is_leg_for_symbol
    from automate.utils.wallet import get_charge_rates

    rates = get_charge_rates(user_id)
    db = SessionLocal()
    try:
        own_strategy_ids = {
            row[0] for row in db.query(CustomStrategy.id).filter(
                CustomStrategy.user_id == user_id
            ).all()
        }
        if not own_strategy_ids:
            return []

        legs = db.query(CustomStrategyPosition).filter(
            CustomStrategyPosition.strategy_id.in_(own_strategy_ids),
            CustomStrategyPosition.status == "OPEN",
        ).order_by(CustomStrategyPosition.opened_at.desc()).all()
        if not legs:
            return []

        strategies = {
            s.id: s for s in db.query(CustomStrategy).filter(CustomStrategy.id.in_({l.strategy_id for l in legs})).all()
        }

        brokers = _get_brokers()
        # Batch LTP fetch per broker mode (paper/live) — one HTTP call per
        # mode instead of one per leg, same rationale as live_greeks.py.
        ltp_by_mode: Dict[str, Dict[str, float]] = {}
        if brokers:
            tokens_by_mode: Dict[str, list] = {}
            for leg in legs:
                tokens_by_mode.setdefault(leg.mode, []).append(leg.instrument_key)
            for mode, tokens in tokens_by_mode.items():
                broker = brokers.get(mode)
                if broker is None:
                    continue
                try:
                    ltp_by_mode[mode] = broker.get_ltp_batch(tokens)
                except Exception:
                    ltp_by_mode[mode] = {}

        rows = []
        for leg in legs:
            strategy = strategies.get(leg.strategy_id)
            if strategy is None:
                continue
            symbols = json.loads(strategy.symbols)
            symbol = next((s for s in symbols if _is_leg_for_symbol(leg.instrument_key, s)), symbols[0] if symbols else "?")
            rules = json.loads(strategy.rules_json) if strategy.rules_json else {}
            expiry_mode = (rules.get("expiry") or {}).get("mode", "WEEKLY")
            entry = float(leg.entry_price)
            ltp = ltp_by_mode.get(leg.mode, {}).get(leg.instrument_key)
            sign = -1 if leg.transaction_type == "SELL" else 1
            pnl = None
            net_pnl = None
            if ltp is not None:
                # Gross mark-to-market — the PRIMARY number, matching how
                # every real broker's Positions page (Kite/Upstox included)
                # shows live P&L: pure (LTP - avg) x qty, no charges baked
                # in, since a real cost is only actually incurred once you
                # truly exit. Same convention this codebase already uses
                # for the legacy strangle table — see api/deps.py's
                # compute_mtm_economics(), which likewise exposes gross as
                # `mtm` and net as a SEPARATE `net_mtm` field rather than
                # collapsing them into one number.
                pnl = (ltp - entry) * leg.quantity * sign

                # Net of real transaction costs on the entry ALREADY paid
                # plus the hypothetical exit AT this LTP — "what you'd
                # actually pocket if you closed right now", same math as
                # custom_strategy_scheduler.py's _combined_pnl_pct (what
                # actually decides SL/TP). Exposed as a secondary field,
                # not the primary display number.
                from automate.utils.costs import calculate_options_transaction_cost_breakdown
                exit_side = "BUY" if leg.transaction_type == "SELL" else "SELL"
                entry_costs = calculate_options_transaction_cost_breakdown(entry, leg.quantity, leg.transaction_type, rates)
                exit_costs = calculate_options_transaction_cost_breakdown(ltp, leg.quantity, exit_side, rates)
                net_pnl = pnl - entry_costs["total"] - exit_costs["total"]
            chg_pct = round((ltp - entry) / entry * 100, 2) if ltp is not None and entry else None
            rows.append({
                "id": leg.id,
                "strategy_id": leg.strategy_id,
                "strategy_name": strategy.name,
                "symbol": symbol,
                "mode": leg.mode,
                "option_type": leg.option_type,
                "strike": float(leg.strike) if leg.strike is not None else None,
                "expiry": leg.expiry,
                "expiry_mode": expiry_mode,
                "instrument_type": leg.instrument_type,
                "transaction_type": leg.transaction_type,
                "quantity": leg.quantity,
                "entry_price": entry,
                "ltp": ltp,
                "pnl": round(pnl, 2) if pnl is not None else None,
                "net_pnl": round(net_pnl, 2) if net_pnl is not None else None,
                "chg_pct": chg_pct,
                "opened_at": leg.opened_at.isoformat() if leg.opened_at else None,
            })

        rows.sort(key=lambda r: r["opened_at"] or "", reverse=True)
        return rows
    finally:
        db.close()
