"""
api/routes_performance.py — Trade journal & performance analytics: win
rate, profit factor, max drawdown, Sharpe ratio, equity curve, and
strategy/symbol/day-of-week breakdowns.

Computed entirely from REAL closed positions (CustomStrategyPosition),
not a separate manually-typed log — this module used to be a standalone
in-memory "trade journal" (a module-level dict, wiped on every restart,
that nothing in the actual trading system ever wrote to — every strategy
engine, the scheduler, everything, was completely disconnected from it).
That's gone. "My trade history" now means exactly what the Leaderboard/
Positions pages already show — this module just adds the metrics/curve/
breakdown views on top of the same real data.

Reuses routes_leaderboard.py's basket-grouping logic (_basket_bucket,
_is_leg_for_symbol, _CATEGORY_LABELS) rather than re-deriving it: a
basket (one entry cycle's worth of legs opened together) is the correct
unit for "one trade" here — a 4-leg iron condor closing is ONE trade for
win-rate/profit-factor purposes, not four raw legs.
"""
import json
import statistics
from collections import defaultdict
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.auth import get_current_user
from api.custom_strategy_scheduler import _is_leg_for_symbol
from api.routes_leaderboard import _CATEGORY_LABELS, _basket_bucket
from db.engine import SessionLocal
from db.models import CustomStrategy, CustomStrategyPosition
from utils.pnl import compute_basket_pnl
from utils.wallet import get_charge_rates

router = APIRouter(prefix="/api/performance", tags=["performance"])


def _uid(user: dict) -> int:
    return int(user["sub"])


class PerformanceMetrics(BaseModel):
    """Performance metrics for a user, over some period/mode filter."""
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    total_pnl: float
    average_win: float
    average_loss: float
    profit_factor: float
    max_drawdown: float
    sharpe_ratio: float | None = None
    best_trade: float | None = None
    worst_trade: float | None = None
    equity_curve: list[dict] = []


_PERIOD_DAYS = {"week": 7, "month": 30, "quarter": 90, "year": 365}


def _closed_trades(db, user_id: int, mode: str | None = None) -> list[dict]:
    """
    One row per real, closed trade (a basket of legs opened together —
    see routes_leaderboard.py::_basket_bucket for why baskets, not raw
    legs, are the right unit), sorted oldest-exit-first. `mode` optionally
    restricts to 'paper' or 'live'; None returns both.
    """
    own_strategy_ids = {
        row[0] for row in db.query(CustomStrategy.id).filter(CustomStrategy.user_id == user_id).all()
    }
    if not own_strategy_ids:
        return []

    q = db.query(CustomStrategyPosition).filter(
        CustomStrategyPosition.status == "CLOSED",
        CustomStrategyPosition.strategy_id.in_(own_strategy_ids),
    )
    if mode:
        q = q.filter(CustomStrategyPosition.mode == mode)
    closed_legs = q.all()
    if not closed_legs:
        return []

    strategies = {s.id: s for s in db.query(CustomStrategy).filter(CustomStrategy.id.in_(own_strategy_ids)).all()}
    rates = get_charge_rates(user_id)

    baskets: dict = defaultdict(list)
    for leg in closed_legs:
        strategy = strategies.get(leg.strategy_id)
        if strategy is None:
            continue
        symbol_list = json.loads(strategy.symbols) if isinstance(strategy.symbols, str) else strategy.symbols
        leg_symbol = next((s for s in symbol_list if _is_leg_for_symbol(leg.instrument_key, s)), None)
        if leg_symbol is None:
            continue
        bucket = _basket_bucket(leg.opened_at)
        baskets[(strategy.id, leg_symbol, leg.mode, bucket)].append(leg)

    trades = []
    for (strategy_id, symbol, leg_mode, bucket), legs in baskets.items():
        if any(leg.exit_price is None for leg in legs):
            continue  # same skip-not-crash rule as the leaderboard — a closed leg with no recorded exit price can't be priced
        strategy = strategies[strategy_id]
        result = compute_basket_pnl([
            {"entry_price": leg.entry_price, "exit_price": leg.exit_price,
             "quantity": leg.quantity, "transaction_type": leg.transaction_type,
             "instrument_type": leg.instrument_type}
            for leg in legs
        ], rates)
        entry_at = bucket or legs[0].opened_at
        exit_at = max((leg.closed_at for leg in legs if leg.closed_at), default=None)
        trades.append({
            "strategy": strategy.name,
            "symbol": symbol,
            "mode": leg_mode,
            "category": _CATEGORY_LABELS.get(strategy.instrument_type, "stock"),
            "entry_date": entry_at.isoformat() if entry_at else None,
            "exit_date": exit_at.isoformat() if exit_at else None,
            "legs": len(legs),
            "pnl": round(result["net_pnl"], 2),
        })

    trades.sort(key=lambda t: t["exit_date"] or "")
    return trades


def _filter_period(trades: list[dict], period: str) -> list[dict]:
    """Period can be: 'all', 'today', 'week', 'month', 'quarter', 'year' — filtered on exit_date, since that's when a trade's P&L actually realized."""
    if period == "all":
        return trades
    now = datetime.now()
    if period == "today":
        return [t for t in trades if t["exit_date"] and datetime.fromisoformat(t["exit_date"]).date() == now.date()]
    days = _PERIOD_DAYS.get(period)
    if days is None:
        return trades
    cutoff = now - timedelta(days=days)
    return [t for t in trades if t["exit_date"] and datetime.fromisoformat(t["exit_date"]) >= cutoff]


@router.get("/journal")
def list_trade_entries(
    symbol: str | None = None,
    strategy: str | None = None,
    mode: str | None = None,
    period: str = "all",
    limit: int = 200,
    user: dict = Depends(get_current_user),
):
    """The caller's real trade history — newest-exit-first, with optional filters."""
    db = SessionLocal()
    try:
        trades = _filter_period(_closed_trades(db, _uid(user), mode), period)
    finally:
        db.close()
    if symbol:
        trades = [t for t in trades if t["symbol"] == symbol]
    if strategy:
        trades = [t for t in trades if t["strategy"] == strategy]
    trades = sorted(trades, key=lambda t: t["exit_date"] or "", reverse=True)
    return {"entries": trades[:limit]}


@router.get("/metrics/{user_id}")
def calculate_performance_metrics(
    user_id: int, period: str = "all", mode: str | None = None, user: dict = Depends(get_current_user),
):
    """Real win rate / profit factor / max drawdown / Sharpe / equity curve, computed fresh on every call (no cache — a closed trade should show up immediately, not after some stale TTL)."""
    if user_id != _uid(user):
        raise HTTPException(status_code=404, detail="Not found")

    db = SessionLocal()
    try:
        trades = _filter_period(_closed_trades(db, user_id, mode), period)
    finally:
        db.close()

    if not trades:
        return PerformanceMetrics(
            total_trades=0, winning_trades=0, losing_trades=0, win_rate=0.0, total_pnl=0.0,
            average_win=0.0, average_loss=0.0, profit_factor=0.0, max_drawdown=0.0,
        )

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    total_pnl = sum(t["pnl"] for t in trades)
    total_wins = sum(t["pnl"] for t in wins)
    total_losses = abs(sum(t["pnl"] for t in losses))

    # Equity curve + max drawdown in one pass — trades are already sorted
    # oldest-exit-first, so a running cumulative sum IS the equity curve.
    running = 0.0
    peak = 0.0
    max_drawdown = 0.0
    equity_curve = []
    for t in trades:
        running += t["pnl"]
        peak = max(peak, running)
        max_drawdown = max(max_drawdown, peak - running)
        equity_curve.append({"date": t["exit_date"], "cumulative_pnl": round(running, 2)})

    sharpe_ratio = None
    if len(trades) > 1:
        pnl_values = [t["pnl"] for t in trades]
        stdev = statistics.stdev(pnl_values)
        if stdev > 0:
            sharpe_ratio = round((statistics.mean(pnl_values) / stdev) * (252 ** 0.5), 2)

    return PerformanceMetrics(
        total_trades=len(trades),
        winning_trades=len(wins),
        losing_trades=len(losses),
        win_rate=round(100 * len(wins) / len(trades), 2),
        total_pnl=round(total_pnl, 2),
        average_win=round(total_wins / len(wins), 2) if wins else 0.0,
        average_loss=round(sum(t["pnl"] for t in losses) / len(losses), 2) if losses else 0.0,
        profit_factor=round(total_wins / total_losses, 2) if total_losses > 0 else 0.0,
        max_drawdown=round(max_drawdown, 2),
        sharpe_ratio=sharpe_ratio,
        best_trade=round(max(t["pnl"] for t in trades), 2),
        worst_trade=round(min(t["pnl"] for t in trades), 2),
        equity_curve=equity_curve,
    )


@router.get("/analytics/{user_id}")
def get_performance_analytics(user_id: int, mode: str | None = None, user: dict = Depends(get_current_user)):
    """Strategy / symbol / day-of-week breakdowns over the caller's full real trade history."""
    if user_id != _uid(user):
        raise HTTPException(status_code=404, detail="Not found")

    db = SessionLocal()
    try:
        trades = _closed_trades(db, user_id, mode)
    finally:
        db.close()

    if not trades:
        return {
            "message": "No completed trades found",
            "strategy_breakdown": {}, "symbol_performance": {}, "day_of_week_performance": {},
            "total_trades_analyzed": 0,
        }

    strategy_performance: dict = defaultdict(lambda: {"trades": 0, "wins": 0, "total_pnl": 0.0})
    symbol_performance: dict = defaultdict(lambda: {"trades": 0, "total_pnl": 0.0})
    dow_performance: dict = defaultdict(lambda: {"trades": 0, "total_pnl": 0.0})

    for t in trades:
        sp = strategy_performance[t["strategy"]]
        sp["trades"] += 1
        sp["total_pnl"] += t["pnl"]
        if t["pnl"] > 0:
            sp["wins"] += 1

        symp = symbol_performance[t["symbol"]]
        symp["trades"] += 1
        symp["total_pnl"] += t["pnl"]

        if t["exit_date"]:
            dp = dow_performance[datetime.fromisoformat(t["exit_date"]).strftime("%A")]
            dp["trades"] += 1
            dp["total_pnl"] += t["pnl"]

    for stats in strategy_performance.values():
        stats["win_rate"] = round(100 * stats["wins"] / stats["trades"], 1) if stats["trades"] else 0.0
        stats["average_pnl"] = round(stats["total_pnl"] / stats["trades"], 2) if stats["trades"] else 0.0
        stats["total_pnl"] = round(stats["total_pnl"], 2)
    for stats in symbol_performance.values():
        stats["average_pnl"] = round(stats["total_pnl"] / stats["trades"], 2) if stats["trades"] else 0.0
        stats["total_pnl"] = round(stats["total_pnl"], 2)
    for stats in dow_performance.values():
        stats["average_pnl"] = round(stats["total_pnl"] / stats["trades"], 2) if stats["trades"] else 0.0
        stats["total_pnl"] = round(stats["total_pnl"], 2)

    return {
        "strategy_breakdown": dict(strategy_performance),
        "symbol_performance": dict(symbol_performance),
        "day_of_week_performance": dict(dow_performance),
        "total_trades_analyzed": len(trades),
    }


@router.get("/export/{user_id}")
def export_trade_journal(
    user_id: int, format: str = "json", period: str = "all", mode: str | None = None,
    user: dict = Depends(get_current_user),
):
    """Export the caller's real trade history."""
    if user_id != _uid(user):
        raise HTTPException(status_code=404, detail="Not found")

    db = SessionLocal()
    try:
        trades = _filter_period(_closed_trades(db, user_id, mode), period)
    finally:
        db.close()

    if format == "json":
        return {"trades": trades}
    if format == "csv":
        import csv
        from io import StringIO

        output = StringIO()
        if trades:
            writer = csv.DictWriter(output, fieldnames=trades[0].keys())
            writer.writeheader()
            writer.writerows(trades)
        return {"csv": output.getvalue()}
    raise HTTPException(status_code=400, detail="Unsupported format")
