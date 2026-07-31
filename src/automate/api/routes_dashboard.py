"""
api/routes_dashboard.py — cross-cuts backtest_runs + positions so the
control panel can chart "how does this symbol/strategy do in backtest vs
paper vs live" side by side, instead of those three living in separate,
uncomparable views.
"""
from collections import defaultdict

from fastapi import APIRouter, Depends

from automate.api.auth import get_current_user
from automate.utils.backtest_history import get_latest_backtest_per_symbol
from automate.utils.pnl import compute_strangle_pnl
from automate.utils.position_tracker import get_closed_positions

router = APIRouter(prefix="/api/performance", tags=["performance"])


def _closed_pnl(position: dict) -> float:
    """Net of real Indian F&O charges — see utils/pnl.py."""
    return compute_strangle_pnl(
        position["call_entry_price"], position["put_entry_price"],
        position["call_exit_price"] or 0.0, position["put_exit_price"] or 0.0,
        position["quantity"],
    )["net_pnl"]


@router.get("")
def performance(user: dict = Depends(get_current_user)):
    backtest_by_symbol = {r["symbol"]: r for r in get_latest_backtest_per_symbol()}

    paper_pnl: dict = defaultdict(float)
    paper_trades: dict = defaultdict(int)
    paper_wins: dict = defaultdict(int)
    live_pnl: dict = defaultdict(float)
    live_trades: dict = defaultdict(int)
    live_wins: dict = defaultdict(int)

    for pos in get_closed_positions(limit=100000, user_id=int(user["sub"])):
        pnl = _closed_pnl(pos)
        symbol = pos["symbol"]
        if pos["mode"] == "live":
            live_pnl[symbol] += pnl
            live_trades[symbol] += 1
            live_wins[symbol] += 1 if pnl > 0 else 0
        else:
            paper_pnl[symbol] += pnl
            paper_trades[symbol] += 1
            paper_wins[symbol] += 1 if pnl > 0 else 0

    symbols = sorted(set(backtest_by_symbol) | set(paper_trades) | set(live_trades))

    rows = []
    for symbol in symbols:
        bt = backtest_by_symbol.get(symbol)
        rows.append({
            "symbol": symbol,
            "backtest": None if bt is None else {
                "strategy": bt["strategy_name"], "run_at": bt["run_at"], "cycles": bt["cycles"],
                "win_rate_pct": bt["win_rate_pct"], "total_pnl": bt["total_pnl"], "total_return_pct": bt["total_return_pct"],
            },
            "paper": None if paper_trades[symbol] == 0 else {
                "trades": paper_trades[symbol], "total_pnl": paper_pnl[symbol],
                "win_rate_pct": 100 * paper_wins[symbol] / paper_trades[symbol],
            },
            "live": None if live_trades[symbol] == 0 else {
                "trades": live_trades[symbol], "total_pnl": live_pnl[symbol],
                "win_rate_pct": 100 * live_wins[symbol] / live_trades[symbol],
            },
        })
    return {"rows": rows}
