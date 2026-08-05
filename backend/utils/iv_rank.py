"""
utils/iv_rank.py — IV rank computation from symbol_iv_history (see
api/iv_history_scheduler.py, the only writer of that table).

IV rank = where today's ATM IV sits within its own trailing ~1-year
range, 0-100 (0 = lowest IV seen in the window, 100 = highest — the
standard definition, same as most options platforms). Returns None —
never a fabricated number — when fewer than _MIN_HISTORY_DAYS rows exist
for a symbol; an entry condition checking IV rank treats None as "not
triggered" (see custom_strategy_scheduler.py's _iv_rank_condition_met),
so a strategy with this condition simply never enters until real history
has accumulated, rather than acting on a meaningless rank computed from
a handful of days. The table starts empty for every symbol at whenever
this feature first shipped — there's no historical options-price archive
live to backfill from (see the module docstring on SymbolIvHistory).
"""
from datetime import date, timedelta

from db.engine import SessionLocal
from db.models import SymbolIvHistory

_LOOKBACK_DAYS = 252  # ~1 trading year
_MIN_HISTORY_DAYS = 30  # below this, a "rank" is too noisy/meaningless to act on


def compute_iv_rank(symbol: str, today_iv: float | None = None) -> float | None:
    """
    today_iv: the CURRENT ATM IV to rank, if the caller already solved it
    (avoids a redundant live IV solve — e.g. routes_custom_strategies.py's
    payoff endpoint already computes ATM IV for other reasons). If
    omitted, ranks the most recent STORED history row instead (at worst
    yesterday's close, since today's snapshot isn't written until after
    market close — see iv_history_scheduler.py) — close enough for an
    intraday "IV rank ABOVE/BELOW X" entry gate without a live IV re-solve
    on every tick.
    """
    cutoff = (date.today() - timedelta(days=_LOOKBACK_DAYS)).isoformat()
    db = SessionLocal()
    try:
        rows = db.query(SymbolIvHistory.atm_iv).filter(
            SymbolIvHistory.symbol == symbol, SymbolIvHistory.trade_date >= cutoff,
        ).order_by(SymbolIvHistory.trade_date.asc()).all()
    finally:
        db.close()

    ivs = [float(r[0]) for r in rows]
    if len(ivs) < _MIN_HISTORY_DAYS:
        return None

    current = today_iv if today_iv is not None else ivs[-1]
    lo, hi = min(ivs), max(ivs)
    if hi <= lo:
        return 50.0  # no observed variance in the window — degenerate but well-defined; avoids a divide-by-zero
    return max(0.0, min(100.0, (current - lo) / (hi - lo) * 100.0))


def history_sufficiency(symbol: str) -> dict:
    """{'days': N, 'required': _MIN_HISTORY_DAYS, 'sufficient': bool} — backs the UI's "N/252 days of history" indicator."""
    cutoff = (date.today() - timedelta(days=_LOOKBACK_DAYS)).isoformat()
    db = SessionLocal()
    try:
        count = db.query(SymbolIvHistory).filter(
            SymbolIvHistory.symbol == symbol, SymbolIvHistory.trade_date >= cutoff,
        ).count()
    finally:
        db.close()
    return {"days": count, "required": _MIN_HISTORY_DAYS, "sufficient": count >= _MIN_HISTORY_DAYS}
