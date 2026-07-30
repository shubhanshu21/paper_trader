"""
utils/backtest_history.py — Persists historical backtest runs in MySQL.
"""
from datetime import datetime
from typing import Optional, List
from decimal import Decimal

from automate.db.engine import get_session
from automate.db.models import BacktestRun


def record_backtest_run(
    strategy_name: str, symbol: str, contract_type: str, from_date: str, to_date: str,
    cycles: int, wins: int, win_rate_pct: Optional[float], total_pnl: float, total_return_pct: Optional[float],
) -> int:
    """Record a completed backtest run in the MySQL database."""
    with get_session() as session:
        run = BacktestRun(
            run_at=datetime.now().isoformat(timespec="seconds"),
            strategy_name=strategy_name,
            symbol=symbol.upper(),
            contract_type=contract_type,
            from_date=from_date,
            to_date=to_date,
            cycles=cycles,
            wins=wins,
            win_rate_pct=win_rate_pct,
            total_pnl=total_pnl,
            total_return_pct=total_return_pct,
        )
        session.add(run)
        session.flush()
        return run.id


def get_latest_backtest_per_symbol() -> List[dict]:
    """
    Most recent backtest run for each (strategy, symbol) pair.
    Uses subqueries in SQLAlchemy to resolve the max run_at for each pair.
    """
    with get_session() as session:
        # Subquery to find the latest run_at per (strategy, symbol)
        subq = (
            session.query(
                BacktestRun.strategy_name,
                BacktestRun.symbol,
                from_self=True
            )
            .group_by(BacktestRun.strategy_name, BacktestRun.symbol)
            .with_entities(
                BacktestRun.strategy_name,
                BacktestRun.symbol,
                from_self=False
            )
        )
        # Wait, the SQL is:
        # SELECT b.* FROM backtest_runs b
        # INNER JOIN (
        #     SELECT strategy_name, symbol, MAX(run_at) AS latest
        #     FROM backtest_runs GROUP BY strategy_name, symbol
        # ) latest ON b.strategy_name = latest.strategy_name ...
        
        # Let's write the query clearly using alias or subquery:
        from sqlalchemy import func
        subq = (
            session.query(
                BacktestRun.strategy_name.label("strat"),
                BacktestRun.symbol.label("sym"),
                func.max(BacktestRun.run_at).label("latest")
            )
            .group_by(BacktestRun.strategy_name, BacktestRun.symbol)
            .subquery()
        )
        
        rows = (
            session.query(BacktestRun)
            .join(
                subq,
                (BacktestRun.strategy_name == subq.c.strat) &
                (BacktestRun.symbol == subq.c.sym) &
                (BacktestRun.run_at == subq.c.latest)
            )
            .order_by(BacktestRun.symbol)
            .all()
        )
        
        results = []
        for r in rows:
            d = {}
            for col in r.__table__.columns:
                v = getattr(r, col.name)
                d[col.name] = float(v) if isinstance(v, Decimal) else v
            results.append(d)
        return results
