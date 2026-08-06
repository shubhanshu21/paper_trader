"""
api/routes_iv_screener.py — IV percentile/rank screener across a user's
own watchlist symbols (across every page, not just page 1 — a screener
should cover the whole watchlist).

Reuses the exact same live-ATM-IV solve as api/iv_history_scheduler.py's
daily snapshot job (_snapshot_symbol_iv — forward price from the nearest
future, Black-76, nearest weekly expiry) and the exact same rank/
sufficiency math the IV_RANK entry condition already uses
(utils/iv_rank.py) — this endpoint is a NEW read-only surface for data
that already existed, not a new computation.

A symbol only has a real rank once SymbolIvHistory has accumulated
_MIN_HISTORY_DAYS (utils/iv_rank.py) worth of EOD snapshots for it —
iv_history_scheduler.py only snapshots symbols referenced by an active
(PAPER_TRADING/LIVE) CustomStrategy, so a watchlist symbol with no
strategy ever built on it will show `sufficient: false` / `iv_rank: None`
indefinitely, by the same design as the entry-condition feature this
data already backs — never a fabricated rank.
"""
from fastapi import APIRouter, Depends
from sqlalchemy import select

from api.auth import get_current_user
from api.custom_strategy_scheduler import _get_brokers
from api.iv_history_scheduler import _snapshot_symbol_iv
from db.engine import get_session
from db.models import Instrument, UserWatchlist
from utils.iv_rank import compute_iv_rank, history_sufficiency
from utils.logger import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/api/iv-screener", tags=["iv-screener"])


@router.get("")
def iv_screener(user: dict = Depends(get_current_user)):
    user_id = int(user["sub"])

    # Two separate lookups rather than a SQL-level join — instruments and
    # user_watchlists have mismatched column collations in this DB, which
    # makes a JOIN on instrument_key fail outright (same reason
    # routes_watchlist.py's own get_user_watchlist() already does a
    # per-row session.get(Instrument, ...) instead of a join).
    with get_session() as session:
        instrument_keys = session.execute(
            select(UserWatchlist.instrument_key).where(UserWatchlist.user_id == user_id).distinct()
        ).scalars().all()
        symbols = sorted({
            inst.symbol
            for key in instrument_keys
            if (inst := session.get(Instrument, key)) is not None
        })
    if not symbols:
        return {"rows": []}

    brokers = _get_brokers()
    # Read-only LTP/chain lookups only — the paper broker sees the exact
    # same real market data as live, no reason to need the live broker
    # here (same reasoning iv_history_scheduler.py's own snapshot uses).
    broker = brokers["paper"] if brokers else None

    result = []
    for symbol in symbols:
        today_iv = _snapshot_symbol_iv(broker, symbol) if broker else None
        rank = compute_iv_rank(symbol, today_iv=today_iv)
        suff = history_sufficiency(symbol)
        result.append({
            "symbol": symbol,
            "current_iv": round(today_iv * 100, 2) if today_iv is not None else None,  # as a % (Black-76 returns a decimal)
            "iv_rank": round(rank, 1) if rank is not None else None,
            "history_days": suff["days"],
            "history_required": suff["required"],
            "sufficient": suff["sufficient"],
        })

    # Highest rank first (richest premium relative to its own history —
    # the most interesting candidates for premium-selling entries);
    # symbols without a rank yet sort last, not first.
    result.sort(key=lambda r: (r["iv_rank"] is None, -(r["iv_rank"] or 0)))
    return {"rows": result}
