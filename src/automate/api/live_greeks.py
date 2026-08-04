"""
api/live_greeks.py — shared computation for a custom strategy's live
Black-76 Greeks (see utils/black76.py), used by both the WebSocket push
(ws_custom_strategy_greeks.py — what the frontend actually uses) and the
one-off REST endpoint (routes_custom_strategies.py's GET .../greeks, kept
for manual/curl inspection) so the logic exists in exactly one place.

compute_portfolio_greeks() is the account-wide version — net exposure
across EVERY open leg of EVERY active (PAPER_TRADING/LIVE) strategy this
user owns, not just one strategy's own legs. A strangle that looks
delta-neutral leg-by-leg can still leave the account meaningfully
directional once combined with a second strategy on a correlated
underlying — this is the number that actually answers "how exposed am I
right now," the same portfolio-level view Sensibull's positions dashboard
gives (aggregate Greeks, not per-leg) instead of Kite's plain per-leg list.
"""
import json
from collections import defaultdict
from datetime import date

from automate.db.engine import SessionLocal
from automate.db.models import CustomStrategy, CustomStrategyPosition
from automate.utils import black76
from automate.utils.instrument_cache import InstrumentCache


def compute_live_greeks(strategy_id: int, owner_user_id: int | None = None) -> dict | None:
    """
    Returns None if the strategy doesn't exist (or, when `owner_user_id` is
    given, doesn't belong to that user — same None/404 either way, so a
    caller can't tell "wrong id" from "someone else's strategy"). Returns
    {"legs": [], "net": None, "message": ...} if it exists but has no open
    legs right now — both are normal outcomes, not errors, since this is
    called on a timer regardless of what state the strategy happens to be in.
    """
    from automate.api.custom_strategy_scheduler import (
        _get_brokers,
        _is_leg_for_symbol,
        _mode_for_status,
    )

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

        futures_price_by_symbol: dict[str, float | None] = {}
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


def compute_portfolio_greeks(owner_user_id: int) -> dict:
    """
    Net Black-76 Greeks across EVERY open leg of EVERY active
    (PAPER_TRADING/LIVE) strategy `owner_user_id` owns — see module
    docstring. Always returns a dict (never None — unlike
    compute_live_greeks, there's no single strategy_id that could be
    "not found"); "net": None plus a "message" means a normal empty
    state (no active strategies, or none with open legs right now), not
    an error.
    """
    from automate.api.custom_strategy_scheduler import (
        _get_brokers,
        _is_leg_for_symbol,
        _mode_for_status,
    )

    db = SessionLocal()
    try:
        strategies = db.query(CustomStrategy).filter(
            CustomStrategy.user_id == owner_user_id,
            CustomStrategy.status.in_(["PAPER_TRADING", "LIVE"]),
        ).all()
        if not strategies:
            return {"net": None, "by_strategy": [], "open_legs_count": 0, "message": "No active strategies."}

        strategy_by_id = {s.id: s for s in strategies}
        open_legs = db.query(CustomStrategyPosition).filter(
            CustomStrategyPosition.strategy_id.in_(list(strategy_by_id.keys())),
            CustomStrategyPosition.status == "OPEN",
        ).all()
        if not open_legs:
            return {"net": None, "by_strategy": [], "open_legs_count": 0, "message": "No open legs across any active strategy."}

        brokers = _get_brokers()
        if brokers is None:
            return {"net": None, "by_strategy": [], "open_legs_count": 0, "message": "Broker connection not ready yet."}

        # Batch the LTP lookup per broker (paper vs live legs need their
        # own broker) rather than per leg, same reasoning as
        # compute_live_greeks — this just also spans multiple strategies'
        # legs in one pass instead of one strategy's.
        legs_by_mode: dict[str, list] = defaultdict(list)
        for leg in open_legs:
            legs_by_mode[_mode_for_status(strategy_by_id[leg.strategy_id].status)].append(leg)

        now_prices: dict[str, float | None] = {}
        for mode, mode_legs in legs_by_mode.items():
            now_prices.update(brokers[mode].get_ltp_batch([leg.instrument_key for leg in mode_legs]))

        instrument_cache = InstrumentCache()
        today = date.today()
        futures_price_cache: dict[str, float | None] = {}

        def _futures_price(symbol: str, broker) -> float | None:
            if symbol not in futures_price_cache:
                resolved = instrument_cache.resolve_nearest_future_key(symbol)
                futures_price_cache[symbol] = broker.get_ltp(resolved[0]) if resolved else None
            return futures_price_cache[symbol]

        net_delta = net_gamma = net_theta = net_vega = 0.0
        per_strategy: dict[int, dict] = {}
        for leg in open_legs:
            strategy = strategy_by_id[leg.strategy_id]
            broker = brokers[_mode_for_status(strategy.status)]
            symbols = json.loads(strategy.symbols)
            leg_symbol = next((s for s in symbols if _is_leg_for_symbol(leg.instrument_key, s)), None)
            F = _futures_price(leg_symbol, broker) if leg_symbol else None
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
            leg_delta = leg_gamma = leg_theta = leg_vega = 0.0
            if g:
                leg_delta = g["delta"] * leg.quantity * sign
                leg_gamma = g["gamma"] * leg.quantity * sign
                leg_theta = g["theta"] * leg.quantity * sign
                leg_vega = g["vega"] * leg.quantity * sign
                net_delta += leg_delta
                net_gamma += leg_gamma
                net_theta += leg_theta
                net_vega += leg_vega

            entry = per_strategy.setdefault(strategy.id, {
                "strategy_id": strategy.id, "name": strategy.name, "status": strategy.status,
                "delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "open_legs": 0,
            })
            entry["delta"] += leg_delta
            entry["gamma"] += leg_gamma
            entry["theta"] += leg_theta
            entry["vega"] += leg_vega
            entry["open_legs"] += 1

        return {
            "net": {
                "delta": round(net_delta, 2), "gamma": round(net_gamma, 4),
                "theta": round(net_theta, 2), "vega": round(net_vega, 2),
            },
            "by_strategy": [
                {**v, "delta": round(v["delta"], 2), "gamma": round(v["gamma"], 4),
                 "theta": round(v["theta"], 2), "vega": round(v["vega"], 2)}
                for v in per_strategy.values()
            ],
            "open_legs_count": len(open_legs),
        }
    finally:
        db.close()


def compute_portfolio_margin(owner_user_id: int) -> dict:
    """
    Real broker-NETTED margin (₹) currently blocked across EVERY open leg
    of EVERY active (PAPER_TRADING/LIVE) strategy `owner_user_id` owns —
    split by paper vs live, the same two independent capital pools
    routes_custom_strategies.py's GET /{id}/margin distinguishes at a
    single-strategy level (a strategy's OWN legs there are a hypothetical
    "if I entered this," not yet-open positions).

    ONE basket call per mode (not per strategy) so the exchange's own
    hedge-benefit netting applies across every strategy's legs together —
    e.g. two strategies each short a NIFTY call on the same underlying
    net down further combined than either alone would suggest.

    Returns {"paper": {"margin_required": float|None, "open_legs": int},
    "live": {...}} — margin_required is None (never guessed/estimated)
    whenever the real broker basket call is unavailable for that mode
    (no active legs there, broker not connected, or the API call itself
    failed) — same "don't guess" contract get_basket_required_margin's
    own docstring documents.
    """
    from automate.api.custom_strategy_scheduler import _get_brokers, _mode_for_status

    result = {
        "paper": {"margin_required": None, "open_legs": 0},
        "live": {"margin_required": None, "open_legs": 0},
    }

    db = SessionLocal()
    try:
        strategies = db.query(CustomStrategy).filter(
            CustomStrategy.user_id == owner_user_id,
            CustomStrategy.status.in_(["PAPER_TRADING", "LIVE"]),
        ).all()
        if not strategies:
            return result

        strategy_by_id = {s.id: s for s in strategies}
        open_legs = db.query(CustomStrategyPosition).filter(
            CustomStrategyPosition.strategy_id.in_(list(strategy_by_id.keys())),
            CustomStrategyPosition.status == "OPEN",
        ).all()
        if not open_legs:
            return result

        brokers = _get_brokers()
        if brokers is None:
            return result

        legs_by_mode: dict[str, list] = defaultdict(list)
        for leg in open_legs:
            legs_by_mode[_mode_for_status(strategy_by_id[leg.strategy_id].status)].append(leg)

        for mode, mode_legs in legs_by_mode.items():
            result[mode]["open_legs"] = len(mode_legs)
            broker = brokers.get(mode)
            if broker is None:
                continue
            basket = [
                {"instrument_key": leg.instrument_key, "quantity": leg.quantity, "transaction_type": leg.transaction_type, "product": "D"}
                for leg in mode_legs
            ]
            margin = broker.get_basket_required_margin(basket)
            result[mode]["margin_required"] = round(margin, 2) if margin is not None else None

        return result
    finally:
        db.close()
