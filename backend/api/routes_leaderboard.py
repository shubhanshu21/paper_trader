"""
api/routes_leaderboard.py — Strategy + symbol performance leaderboard.

Reads closed legs from CustomStrategyPosition (the Custom Strategy
Builder's generic multi-leg position table — see db.models.py) rather
than the legacy fixed-shape Position table, which belonged to the old
hand-written strangle-only strategies (run_daemon.py/run_strategy.py)
that this platform has moved on from. Groups legs back into baskets
(one entry cycle of a strategy+symbol) to produce a ranked, net-of-real-
charges P&L leaderboard, by asset class (stocks, indices, commodities).
"""
import json
from collections import defaultdict
from datetime import date as date_cls

from fastapi import APIRouter, Depends

from api.auth import get_current_user
from api.custom_strategy_scheduler import _is_leg_for_symbol
from db.engine import SessionLocal
from db.models import CustomStrategy, CustomStrategyPosition
from utils.pnl import compute_basket_pnl
from utils.wallet import get_charge_rates

router = APIRouter(prefix="/api/leaderboard", tags=["leaderboard"])

# NSE indices traded as F&O
_INDEX_SYMBOLS = {
    "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY",
    "SENSEX", "BANKEX", "NIFTYNXT50",
}

# MCX / commodity symbols
_COMMODITY_SYMBOLS = {
    "GOLD", "GOLDM", "GOLDPETAL", "SILVER", "SILVERM", "SILVERMIC",
    "CRUDEOIL", "CRUDEOILM", "NATURALGAS", "NATURALGASM",
    "COPPER", "ZINC", "LEAD", "ALUMINIUM", "NICKEL",
}


def _classify(symbol: str) -> str:
    s = symbol.upper()
    if s in _INDEX_SYMBOLS:
        return "index"
    if s in _COMMODITY_SYMBOLS:
        return "commodity"
    return "stock"


@router.get("")
def leaderboard(user: dict = Depends(get_current_user)):
    """
    Returns a ranked list of (strategy, symbol) P&L entries, scoped to
    strategies owned by the logged-in user, grouped by asset class.
    Only CLOSED legs count — an open basket has no realized P&L yet.
    """
    db = SessionLocal()
    try:
        own_strategy_ids = {
            row[0] for row in db.query(CustomStrategy.id).filter(
                CustomStrategy.user_id == int(user["sub"])
            ).all()
        }
        if not own_strategy_ids:
            return {"rows": []}

        closed_legs = db.query(CustomStrategyPosition).filter(
            CustomStrategyPosition.status == "CLOSED",
            CustomStrategyPosition.strategy_id.in_(own_strategy_ids),
        ).all()
        if not closed_legs:
            return {"rows": []}

        rates = get_charge_rates(int(user["sub"]))

        strategies = {
            s.id: s for s in db.query(CustomStrategy).filter(CustomStrategy.id.in_(own_strategy_ids)).all()
        }

        # Bucket legs into baskets: (strategy_id, symbol, mode, entry day).
        # There's no explicit basket_id column — legs of one entry are
        # inserted together in the same commit (see
        # custom_strategy_scheduler.py's _try_entry), so bucketing by the
        # calendar day they were opened on safely separates distinct
        # entry cycles (a symbol enters at most once per expiry cycle —
        # weekly/monthly — never more than once on the same day).
        baskets: dict = defaultdict(list)
        for leg in closed_legs:
            strategy = strategies.get(leg.strategy_id)
            if strategy is None:
                continue
            symbols = strategy.symbols
            symbol_list = json.loads(symbols) if isinstance(symbols, str) else symbols
            leg_symbol = next((s for s in symbol_list if _is_leg_for_symbol(leg.instrument_key, s)), None)
            if leg_symbol is None:
                continue
            opened_day = leg.opened_at.date() if leg.opened_at else date_cls.today()
            key = (strategy.id, leg_symbol, leg.mode, opened_day)
            baskets[key].append(leg)

        # Aggregate baskets up to (strategy_name, symbol, mode)
        pnl_map: dict = defaultdict(float)
        trades_map: dict = defaultdict(int)
        wins_map: dict = defaultdict(int)

        for (strategy_id, symbol, mode, _day), legs in baskets.items():
            strategy = strategies[strategy_id]
            result = compute_basket_pnl([
                {
                    "entry_price": leg.entry_price, "exit_price": leg.exit_price,
                    "quantity": leg.quantity, "transaction_type": leg.transaction_type,
                    "instrument_type": leg.instrument_type,
                }
                for leg in legs
            ], rates)
            key = (strategy.name, symbol, mode)
            pnl_map[key] += result["net_pnl"]
            trades_map[key] += 1
            if result["net_pnl"] > 0:
                wins_map[key] += 1

        rows = []
        for (strategy_name, symbol, mode), total_pnl in pnl_map.items():
            trades = trades_map[(strategy_name, symbol, mode)]
            wins = wins_map[(strategy_name, symbol, mode)]
            rows.append({
                "strategy": strategy_name,
                "symbol": symbol,
                "mode": mode,              # 'paper' | 'live'
                "category": _classify(symbol),  # 'stock' | 'index' | 'commodity'
                "total_pnl": round(total_pnl, 2),
                "trades": trades,
                "wins": wins,
                "win_rate_pct": round(100 * wins / trades, 1) if trades else 0.0,
                "avg_pnl": round(total_pnl / trades, 2) if trades else 0.0,
            })

        rows.sort(key=lambda r: r["total_pnl"], reverse=True)
        return {"rows": rows}
    finally:
        db.close()
