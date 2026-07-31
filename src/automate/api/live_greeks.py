"""
api/live_greeks.py — shared computation for a custom strategy's live
Black-76 Greeks (see utils/black76.py), used by both the WebSocket push
(ws_custom_strategy_greeks.py — what the frontend actually uses) and the
one-off REST endpoint (routes_custom_strategies.py's GET .../greeks, kept
for manual/curl inspection) so the logic exists in exactly one place.
"""
import json
from datetime import date
from typing import Dict, Optional

from automate.db.engine import SessionLocal
from automate.db.models import CustomStrategy, CustomStrategyPosition
from automate.utils import black76
from automate.utils.instrument_cache import InstrumentCache


def compute_live_greeks(strategy_id: int, owner_user_id: Optional[int] = None) -> Optional[dict]:
    """
    Returns None if the strategy doesn't exist (or, when `owner_user_id` is
    given, doesn't belong to that user — same None/404 either way, so a
    caller can't tell "wrong id" from "someone else's strategy"). Returns
    {"legs": [], "net": None, "message": ...} if it exists but has no open
    legs right now — both are normal outcomes, not errors, since this is
    called on a timer regardless of what state the strategy happens to be in.
    """
    from automate.api.custom_strategy_scheduler import _get_brokers, _mode_for_status, _is_leg_for_symbol

    db = SessionLocal()
    try:
        query = db.query(CustomStrategy).filter(CustomStrategy.id == strategy_id)
        if owner_user_id is not None:
            query = query.filter(CustomStrategy.user_id == owner_user_id)
        db_strategy = query.first()
        if not db_strategy:
            return None

        open_legs = db.query(CustomStrategyPosition).filter(
            CustomStrategyPosition.strategy_id == strategy_id,
            CustomStrategyPosition.status == "OPEN",
        ).all()
        if not open_legs:
            return {"strategy_id": strategy_id, "legs": [], "net": None, "message": "No open legs to price."}

        brokers = _get_brokers()
        if brokers is None:
            return {"strategy_id": strategy_id, "legs": [], "net": None, "message": "Broker connection not ready yet."}
        broker = brokers[_mode_for_status(db_strategy.status)]
        symbols = json.loads(db_strategy.symbols)
        instrument_cache = InstrumentCache()
        today = date.today()

        tokens = [leg.instrument_key for leg in open_legs]
        now_prices = broker.get_ltp_batch(tokens)

        futures_price_by_symbol: Dict[str, Optional[float]] = {}
        for symbol in symbols:
            resolved = instrument_cache.resolve_nearest_future_key(symbol)
            futures_price_by_symbol[symbol] = broker.get_ltp(resolved[0]) if resolved else None

        results = []
        net_delta = net_gamma = net_theta = net_vega = 0.0
        for leg in open_legs:
            leg_symbol = next((s for s in symbols if _is_leg_for_symbol(leg.instrument_key, s)), None)
            F = futures_price_by_symbol.get(leg_symbol) if leg_symbol else None
            market_price = now_prices.get(leg.instrument_key)
            g = None
            if F is not None and market_price is not None and leg.expiry and leg.option_type:
                days_to_expiry = (date.fromisoformat(leg.expiry) - today).days
                T = max(days_to_expiry, 1) / 365.0
                g = black76.compute_greeks_from_market_price(
                    F=F, K=float(leg.strike), T=T, r=black76.DEFAULT_RISK_FREE_RATE,
                    market_price=market_price, option_type=leg.option_type,
                )
            sign = -1 if leg.transaction_type == "SELL" else 1
            if g:
                net_delta += g["delta"] * leg.quantity * sign
                net_gamma += g["gamma"] * leg.quantity * sign
                net_theta += g["theta"] * leg.quantity * sign
                net_vega += g["vega"] * leg.quantity * sign
            results.append({
                "leg_index": leg.leg_index,
                "symbol": leg_symbol,
                "option_type": leg.option_type,
                "strike": float(leg.strike) if leg.strike is not None else None,
                "expiry": leg.expiry,
                "transaction_type": leg.transaction_type,
                "quantity": leg.quantity,
                "current_price": market_price,
                "futures_price": F,
                "greeks": g,
            })

        return {
            "strategy_id": strategy_id,
            "legs": results,
            "net": {
                "delta": round(net_delta, 2),
                "gamma": round(net_gamma, 4),
                "theta": round(net_theta, 2),
                "vega": round(net_vega, 2),
            },
        }
    finally:
        db.close()
