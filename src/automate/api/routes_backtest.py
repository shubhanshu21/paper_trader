"""
api/routes_backtest.py — trigger a backtest and return STRUCTURED results
for the control panel's grid, instead of raw CLI text.

Routing mirrors backtest/__main__.py exactly (recent dates -> real minute
-candle single-trade engine; older dates -> the bhavcopy multi-cycle
engine) so the API and a human running the CLI command directly always
agree on which backend actually served a given date range.

The historical (multi-cycle) path is called IN-PROCESS via
HistoricalCycleEngine directly — same class backtest/historical_engine.py's
own main() uses — so the API gets the raw per-cycle dicts instead of
having to parse the CLI's printed Rich table back out of stdout text. The
recent-date path still shells out (it needs to download real minute data
first via scripts/download_real_history.py, then run engine.py) and its
report isn't yet restructured into a grid — it's returned as raw text for
the frontend to display as-is.
"""
import statistics
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from automate.utils.costs import sum_breakdowns

router = APIRouter(prefix="/api/backtest", tags=["backtest"])

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
LIVE_DATA_MAX_DAYS = 30  # Same cutoff as backtest/__main__.py — Upstox's real 1-minute history limit.
_TIMEOUT_SEC = 300
_EXIT_REASON_LABELS = {"EXPIRY": "Pre-expiry exit", "TAKE_PROFIT": "Take profit", "STOP_LOSS": "Stop loss"}
_INDEX_SYMBOLS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"}


class BacktestRequest(BaseModel):
    symbol: str
    type: str | None = None
    # No hand-written strategies are registered anymore (see
    # strategies/registry.py) — this field only still matters for the
    # custom_strategy_id path below, which overwrites it from the DB row.
    strategy: str = "custom"
    custom_strategy_id: int | None = None  # Custom strategy ID if using custom strategy
    from_date: str
    to_date: str
    num_lots: int = 1
    take_profit_pct: float | None = None
    stop_loss_pct: float | None = None
    exit_days_before_expiry: int = 1
    # Realistic fill simulation parameters
    slippage_bps: float | None = 5.0  # Slippage in basis points
    partial_fill_pct: float | None = 100.0  # Percentage of order that fills
    market_impact: float | None = 0.1  # Market impact factor
    latency_ms: int | None = 50  # Simulated execution latency in milliseconds


def _apply_realistic_fills(results: list, req: BacktestRequest) -> list:
    """Apply realistic fill simulation to backtest results."""
    slippage_bps = req.slippage_bps or 5.0
    partial_fill_pct = req.partial_fill_pct or 100.0
    market_impact = req.market_impact or 0.1
    
    for result in results:
        # Apply slippage to entry and exit prices
        if result.get('gross_pnl'):
            # Calculate slippage impact
            slippage_factor = slippage_bps / 10000  # Convert bps to decimal
            slippage_impact = abs(result['gross_pnl']) * slippage_factor * market_impact
            
            # Apply partial fill reduction
            fill_factor = partial_fill_pct / 100.0
            
            # Adjust P&L for realistic fills
            result['gross_pnl'] = result['gross_pnl'] * fill_factor - slippage_impact
            result['net_pnl'] = result['gross_pnl'] - result.get('charges', {}).get('total', 0)
            
            # Add fill simulation metadata
            result['fill_simulation'] = {
                'slippage_bps': slippage_bps,
                'partial_fill_pct': partial_fill_pct,
                'market_impact': market_impact,
                'slippage_impact': slippage_impact,
                'fill_factor': fill_factor
            }
    
    return results


def _raw_stats(results: list) -> dict:
    """Numeric (not pre-formatted) aggregate stats — the frontend does its own currency/color formatting."""
    if not results:
        return {
            "cycles": 0, "wins": 0, "win_rate_pct": None, "total_pnl": 0.0,
            "average_pnl": 0.0, "best_pnl": None, "worst_pnl": None,
            "capital_needed": 0.0, "total_return_pct": None,
            "total_gross_pnl": 0.0, "total_charges": 0.0, "charges": None,
        }
    net = [r["net_pnl"] for r in results]
    gross = [r["gross_pnl"] for r in results]
    capital = [r["capital_needed"] for r in results]
    wins = sum(1 for x in net if x > 0)
    max_capital = max(capital) if capital else 0.0
    charges = sum_breakdowns(*[r["charges"] for r in results])
    return {
        "cycles": len(results),
        "wins": wins,
        "win_rate_pct": 100 * wins / len(results),
        "total_pnl": sum(net),
        "average_pnl": statistics.mean(net),
        "best_pnl": max(net),
        "worst_pnl": min(net),
        "capital_needed": max_capital,
        "total_return_pct": (sum(net) / max_capital * 100) if max_capital else 0.0,
        "total_gross_pnl": sum(gross),
        "total_charges": charges["total"],
        "charges": charges,
    }


def _run_historical(req: BacktestRequest, contract_type: str) -> dict:
    from automate.backtest.historical_engine import HistoricalCycleEngine
    from automate.compliance.sebi_rules import init_market_calendar
    from automate.db.engine import get_db
    from automate.db.models import CustomStrategy

    init_market_calendar(force=False)

    # Load custom strategy if provided
    custom_strategy = None
    if req.custom_strategy_id:
        db = next(get_db())
        custom_strategy = db.query(CustomStrategy).filter(CustomStrategy.id == req.custom_strategy_id).first()
        if custom_strategy:
            # Use custom strategy parameters
            req.strategy = custom_strategy.strategy_type
            req.num_lots = custom_strategy.num_lots
            req.take_profit_pct = custom_strategy.take_profit_pct
            req.stop_loss_pct = custom_strategy.stop_loss_pct
            req.exit_days_before_expiry = custom_strategy.exit_days_before_expiry

    strategy_kwargs = {"exit_days_before_expiry": req.exit_days_before_expiry}
    if req.take_profit_pct is not None:
        strategy_kwargs["take_profit_pct"] = req.take_profit_pct
    if req.stop_loss_pct is not None:
        strategy_kwargs["stop_loss_pct"] = req.stop_loss_pct

    engine = HistoricalCycleEngine(
        symbol=req.symbol,
        strategy=req.strategy,
        num_lots=req.num_lots,
        strike_step=None,  # resolved dynamically from the real instrument master, same as live/paper.
        option_instrument="OPTIDX" if contract_type == "index" else "OPTSTK",
        future_instrument="FUTIDX" if contract_type == "index" else "FUTSTK",
        strategy_kwargs=strategy_kwargs,
    )
    results = engine.run(req.from_date, req.to_date)
    
    # Apply realistic fill simulation
    results = _apply_realistic_fills(results, req)
    
    liquid_results = [r for r in results if r["liquid"]]
    stats_all = _raw_stats(results)

    if stats_all["cycles"] > 0:
        from automate.utils.backtest_history import record_backtest_run
        record_backtest_run(
            strategy_name=req.strategy, symbol=req.symbol, contract_type=contract_type,
            from_date=req.from_date, to_date=req.to_date,
            cycles=stats_all["cycles"], wins=stats_all["wins"], win_rate_pct=stats_all["win_rate_pct"],
            total_pnl=stats_all["total_pnl"], total_return_pct=stats_all["total_return_pct"],
        )

        # Update custom strategy performance if backtesting a custom strategy
        if custom_strategy:
            custom_strategy.backtest_return_pct = stats_all["total_return_pct"]
            db.commit()

    trades = [
        {
            "index": i,
            "entry_date": r["entry_date"],
            "exit_date": r["exit_date"],
            "call_strike": r["call_strike"],
            "put_strike": r["put_strike"],
            "capital_needed": r["capital_needed"],
            "gross_pnl": r["gross_pnl"],
            "net_pnl": r["net_pnl"],
            "charges": r["charges"],
            "return_pct": (r["net_pnl"] / r["capital_needed"] * 100) if r["capital_needed"] else 0.0,
            "exit_reason": _EXIT_REASON_LABELS.get(r["exit_reason"], r["exit_reason"]),
            "liquid": r["liquid"],
        }
        for i, r in enumerate(results, 1)
    ]

    return {
        "mode": "historical",
        "meta": {"symbol": req.symbol.upper(), "type": contract_type, "strategy": req.strategy,
                  "from_date": req.from_date, "to_date": req.to_date},
        "trades": trades,
        "stats_all": stats_all,
        "stats_liquid": _raw_stats(liquid_results),
    }


def _run_recent(req: BacktestRequest, contract_type: str) -> dict:
    import subprocess
    import sys

    days = max((date.today() - datetime.strptime(req.from_date, "%Y-%m-%d").date()).days, 1)
    download_cmd = [sys.executable, "scripts/download_real_history.py", "--symbol", req.symbol.upper(), "--days", str(days)]
    download = subprocess.run(download_cmd, cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=_TIMEOUT_SEC)
    if download.returncode != 0:
        return {"mode": "raw", "command": " ".join(download_cmd), "exit_code": download.returncode,
                "stdout": download.stdout, "stderr": download.stderr}

    cmd = [sys.executable, "-m", "automate.backtest.engine", "--symbol", req.symbol.upper(),
           "--strategy", req.strategy, "--num-lots", str(req.num_lots)]
    proc = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=_TIMEOUT_SEC)
    return {"mode": "raw", "command": " ".join(cmd), "exit_code": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}


@router.post("")
def run_backtest(req: BacktestRequest):
    symbol = req.symbol.upper()
    contract_type = req.type or ("index" if symbol in _INDEX_SYMBOLS else "stock")
    from_d = datetime.strptime(req.from_date, "%Y-%m-%d").date()

    try:
        if from_d >= date.today() - timedelta(days=LIVE_DATA_MAX_DAYS):
            return _run_recent(req, contract_type)
        return _run_historical(req, contract_type)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Backtest failed: {exc}") from exc
