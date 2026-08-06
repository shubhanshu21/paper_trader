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
from datetime import datetime as datetime_cls

from fastapi import APIRouter, Depends

from api.auth import get_current_user
from api.custom_strategy_scheduler import _is_leg_for_symbol
from db.engine import SessionLocal
from db.models import CustomStrategy, CustomStrategyPosition
from utils.logger import get_logger
from utils.pnl import compute_basket_pnl
from utils.wallet import get_charge_rates

log = get_logger(__name__)

router = APIRouter(prefix="/api/leaderboard", tags=["leaderboard"])

# Asset class -> the API's lowercase category label. instrument_type is the
# user's own choice at creation time (see StrategyBuilderModal.tsx's
# "Instrument Type" step) and is authoritative — classifying by guessing
# from the symbol name (the old approach) is both redundant and fragile:
# any commodity/stock symbol missing from a hardcoded list would silently
# misclassify as "stock" forever, with no way to notice short of eyeballing
# the table.
_CATEGORY_LABELS = {"INDEX": "index", "STOCK": "stock", "COMMODITY": "commodity"}


# Legs of one real entry are inserted in the same tick (see e.g.
# custom_strategy_scheduler.py's _try_entry / smart_condor_engine.py's
# entry path) and so land within a few seconds of each other — but several
# engines can legitimately open a NEW basket for the same (strategy,
# symbol, mode) again on the SAME calendar day: SESSION_SELLER trades two
# sessions a day, and SMART_CONDOR/DELTA_NEUTRAL_STRANGLE/OTM_PUT_ROLL all
# close+reopen legs mid-cycle as an adjustment/roll. Bucketing by calendar
# day alone (the original approach) silently merges these distinct trade
# events into one row, undercounting `trades` and corrupting win_rate_pct/
# avg_pnl (total_pnl stays numerically right since it's a plain sum).
# Rounding to a 15-minute window instead keeps one real entry's legs
# together (they land within the same window) while correctly splitting
# entries that are actually hours apart — without needing a schema change
# (there's no basket_id column) for this row-count-heavy accuracy.
_BASKET_BUCKET_MINUTES = 15


def _basket_bucket(opened_at) -> datetime_cls | None:
    if opened_at is None:
        return None
    floored_minute = (opened_at.minute // _BASKET_BUCKET_MINUTES) * _BASKET_BUCKET_MINUTES
    return opened_at.replace(minute=floored_minute, second=0, microsecond=0)


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

        # Bucket legs into baskets: (strategy_id, symbol, mode, entry
        # window) — see _basket_bucket()'s docstring for why this is a
        # 15-minute-rounded timestamp rather than a calendar day.
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
            bucket = _basket_bucket(leg.opened_at)
            key = (strategy.id, leg_symbol, leg.mode, bucket)
            baskets[key].append(leg)

        # Aggregate baskets up to (strategy_name, symbol, mode)
        pnl_map: dict = defaultdict(float)
        trades_map: dict = defaultdict(int)
        wins_map: dict = defaultdict(int)

        for (strategy_id, symbol, mode, _bucket), legs in baskets.items():
            strategy = strategies[strategy_id]
            # A CLOSED leg can still have exit_price == None (see
            # custom_strategy_scheduler.py::_close_leg — the fallback price
            # lookup can itself miss) — compute_basket_pnl() does a bare
            # float(exit_price) with no null guard (correctly so; it's also
            # used for open-position MTM where a None there would be a real
            # bug), so skip a basket with a null-priced leg here rather than
            # letting one bad row 500 the whole leaderboard for this user.
            if any(leg.exit_price is None for leg in legs):
                log.warning(
                    "leaderboard: skipping basket (strategy=%s symbol=%s) — a closed leg has no exit_price recorded",
                    strategy_id, symbol,
                )
                continue
            result = compute_basket_pnl([
                {
                    "entry_price": leg.entry_price, "exit_price": leg.exit_price,
                    "quantity": leg.quantity, "transaction_type": leg.transaction_type,
                    "instrument_type": leg.instrument_type,
                }
                for leg in legs
            ], rates)
            # Keyed by strategy_id, not name — two strategies can share a
            # display name (e.g. one deleted and rebuilt with the same
            # name), and name-keying would silently merge their P&L into
            # one row.
            key = (strategy_id, symbol, mode)
            pnl_map[key] += result["net_pnl"]
            trades_map[key] += 1
            if result["net_pnl"] > 0:
                wins_map[key] += 1

        rows = []
        for (strategy_id, symbol, mode), total_pnl in pnl_map.items():
            trades = trades_map[(strategy_id, symbol, mode)]
            wins = wins_map[(strategy_id, symbol, mode)]
            strategy = strategies[strategy_id]
            rows.append({
                "strategy": strategy.name,
                "symbol": symbol,
                "mode": mode,              # 'paper' | 'live'
                "category": _CATEGORY_LABELS.get(strategy.instrument_type, "stock"),
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
