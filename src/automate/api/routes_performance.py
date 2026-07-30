"""
api/routes_performance.py — Trade journaling and performance analytics API.

Provides comprehensive trade tracking, performance metrics, and
analytics for improving trading strategies and discipline.
"""
from datetime import datetime, timedelta
from typing import Optional, List, Dict
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/performance", tags=["performance"])


class TradeJournalEntry(BaseModel):
    """Individual trade journal entry."""
    symbol: str
    strategy: str
    entry_date: str
    exit_date: Optional[str] = None
    side: str  # "BUY" or "SELL"
    quantity: int
    entry_price: float
    exit_price: Optional[float] = None
    pnl: Optional[float] = None
    notes: Optional[str] = None
    tags: Optional[List[str]] = None
    user_id: Optional[int] = None


class PerformanceMetrics(BaseModel):
    """Performance metrics for a user or strategy."""
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    total_pnl: float
    average_win: float
    average_loss: float
    profit_factor: float
    max_drawdown: float
    sharpe_ratio: Optional[float] = None
    best_trade: Optional[float] = None
    worst_trade: Optional[float] = None


# In-memory storage for trade journal (in production, use database)
trade_journal: dict = {}
performance_cache: dict = {}


@router.post("/journal")
def add_trade_entry(entry: TradeJournalEntry):
    """Add a trade entry to the journal."""
    entry_id = f"trade_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    
    # Calculate P&L if exit price is provided
    if entry.exit_price and not entry.pnl:
        if entry.side == "BUY":
            entry.pnl = (entry.exit_price - entry.entry_price) * entry.quantity
        else:
            entry.pnl = (entry.entry_price - entry.exit_price) * entry.quantity
    
    trade_journal[entry_id] = {
        "entry_id": entry_id,
        **entry.dict(),
        "created_at": datetime.now().isoformat()
    }
    
    # Invalidate performance cache for this user
    if entry.user_id:
        performance_cache.pop(entry.user_id, None)
    
    return {"entry_id": entry_id, "status": "created"}


@router.get("/journal/{entry_id}")
def get_trade_entry(entry_id: str):
    """Get a specific trade journal entry."""
    if entry_id not in trade_journal:
        raise HTTPException(status_code=404, detail="Trade entry not found")
    return trade_journal[entry_id]


@router.put("/journal/{entry_id}")
def update_trade_entry(entry_id: str, entry: TradeJournalEntry):
    """Update a trade journal entry."""
    if entry_id not in trade_journal:
        raise HTTPException(status_code=404, detail="Trade entry not found")
    
    # Recalculate P&L if exit price changed
    if entry.exit_price and not entry.pnl:
        if entry.side == "BUY":
            entry.pnl = (entry.exit_price - entry.entry_price) * entry.quantity
        else:
            entry.pnl = (entry.entry_price - entry.exit_price) * entry.quantity
    
    trade_journal[entry_id].update({
        **entry.dict(),
        "updated_at": datetime.now().isoformat()
    })
    
    # Invalidate performance cache
    if entry.user_id:
        performance_cache.pop(entry.user_id, None)
    
    return {"entry_id": entry_id, "status": "updated"}


@router.delete("/journal/{entry_id}")
def delete_trade_entry(entry_id: str):
    """Delete a trade journal entry."""
    if entry_id not in trade_journal:
        raise HTTPException(status_code=404, detail="Trade entry not found")
    
    user_id = trade_journal[entry_id].get("user_id")
    del trade_journal[entry_id]
    
    # Invalidate performance cache
    if user_id:
        performance_cache.pop(user_id, None)
    
    return {"entry_id": entry_id, "status": "deleted"}


@router.get("/journal")
def list_trade_entries(
    user_id: Optional[int] = None,
    symbol: Optional[str] = None,
    strategy: Optional[str] = None,
    limit: int = 100
):
    """List trade journal entries with optional filters."""
    entries = list(trade_journal.values())
    
    if user_id:
        entries = [e for e in entries if e["user_id"] == user_id]
    
    if symbol:
        entries = [e for e in entries if e["symbol"] == symbol]
    
    if strategy:
        entries = [e for e in entries if e["strategy"] == strategy]
    
    # Sort by entry date descending
    entries.sort(key=lambda x: x["entry_date"], reverse=True)
    
    return {"entries": entries[:limit]}


@router.get("/metrics/{user_id}")
def calculate_performance_metrics(user_id: int, period: str = "all"):
    """
    Calculate performance metrics for a user.
    
    Period can be: 'all', 'today', 'week', 'month', 'quarter', 'year'
    """
    # Check cache
    cache_key = f"{user_id}_{period}"
    if cache_key in performance_cache:
        return performance_cache[cache_key]
    
    # Get user's trades
    user_trades = [t for t in trade_journal.values() if t["user_id"] == user_id]
    
    # Filter by period
    now = datetime.now()
    if period == "today":
        user_trades = [t for t in user_trades if datetime.fromisoformat(t["entry_date"]).date() == now.date()]
    elif period == "week":
        week_ago = now - timedelta(days=7)
        user_trades = [t for t in user_trades if datetime.fromisoformat(t["entry_date"]) >= week_ago]
    elif period == "month":
        month_ago = now - timedelta(days=30)
        user_trades = [t for t in user_trades if datetime.fromisoformat(t["entry_date"]) >= month_ago]
    elif period == "quarter":
        quarter_ago = now - timedelta(days=90)
        user_trades = [t for t in user_trades if datetime.fromisoformat(t["entry_date"]) >= quarter_ago]
    elif period == "year":
        year_ago = now - timedelta(days=365)
        user_trades = [t for t in user_trades if datetime.fromisoformat(t["entry_date"]) >= year_ago]
    
    # Calculate metrics
    completed_trades = [t for t in user_trades if t["pnl"] is not None]
    
    if not completed_trades:
        return PerformanceMetrics(
            total_trades=0,
            winning_trades=0,
            losing_trades=0,
            win_rate=0.0,
            total_pnl=0.0,
            average_win=0.0,
            average_loss=0.0,
            profit_factor=0.0,
            max_drawdown=0.0
        )
    
    total_trades = len(completed_trades)
    winning_trades = [t for t in completed_trades if t["pnl"] > 0]
    losing_trades = [t for t in completed_trades if t["pnl"] < 0]
    
    wins = len(winning_trades)
    losses = len(losing_trades)
    win_rate = (wins / total_trades) * 100 if total_trades > 0 else 0.0
    
    total_pnl = sum(t["pnl"] for t in completed_trades)
    average_win = sum(t["pnl"] for t in winning_trades) / wins if wins > 0 else 0.0
    average_loss = sum(t["pnl"] for t in losing_trades) / losses if losses > 0 else 0.0
    
    total_wins = sum(t["pnl"] for t in winning_trades)
    total_losses = abs(sum(t["pnl"] for t in losing_trades))
    profit_factor = total_wins / total_losses if total_losses > 0 else 0.0
    
    # Calculate max drawdown
    cumulative_pnl = []
    running_total = 0.0
    for trade in sorted(completed_trades, key=lambda x: x["entry_date"]):
        if trade["pnl"]:
            running_total += trade["pnl"]
            cumulative_pnl.append(running_total)
    
    max_drawdown = 0.0
    if cumulative_pnl:
        peak = cumulative_pnl[0]
        for value in cumulative_pnl:
            if value > peak:
                peak = value
            drawdown = peak - value
            if drawdown > max_drawdown:
                max_drawdown = drawdown
    
    # Calculate Sharpe ratio (simplified)
    sharpe_ratio = None
    if len(completed_trades) > 1:
        import statistics
        pnl_values = [t["pnl"] for t in completed_trades]
        if statistics.stdev(pnl_values) > 0:
            sharpe_ratio = (statistics.mean(pnl_values) / statistics.stdev(pnl_values)) * (252 ** 0.5)
    
    best_trade = max(t["pnl"] for t in completed_trades) if completed_trades else None
    worst_trade = min(t["pnl"] for t in completed_trades) if completed_trades else None
    
    metrics = PerformanceMetrics(
        total_trades=total_trades,
        winning_trades=wins,
        losing_trades=losses,
        win_rate=win_rate,
        total_pnl=total_pnl,
        average_win=average_win,
        average_loss=average_loss,
        profit_factor=profit_factor,
        max_drawdown=max_drawdown,
        sharpe_ratio=sharpe_ratio,
        best_trade=best_trade,
        worst_trade=worst_trade
    )
    
    # Cache result
    performance_cache[cache_key] = metrics
    
    return metrics


@router.get("/analytics/{user_id}")
def get_performance_analytics(user_id: int):
    """
    Get detailed performance analytics including:
    - Strategy breakdown
    - Symbol performance
    - Time-based analysis
    - Win/loss patterns
    """
    user_trades = [t for t in trade_journal.values() if t["user_id"] == user_id and t["pnl"] is not None]
    
    if not user_trades:
        return {"message": "No completed trades found"}
    
    # Strategy breakdown
    strategy_performance: Dict[str, Dict] = {}
    for trade in user_trades:
        strategy = trade["strategy"]
        if strategy not in strategy_performance:
            strategy_performance[strategy] = {
                "trades": 0,
                "wins": 0,
                "total_pnl": 0.0
            }
        strategy_performance[strategy]["trades"] += 1
        strategy_performance[strategy]["total_pnl"] += trade["pnl"]
        if trade["pnl"] > 0:
            strategy_performance[strategy]["wins"] += 1
    
    # Calculate win rates per strategy
    for strategy in strategy_performance:
        stats = strategy_performance[strategy]
        stats["win_rate"] = (stats["wins"] / stats["trades"]) * 100 if stats["trades"] > 0 else 0.0
        stats["average_pnl"] = stats["total_pnl"] / stats["trades"] if stats["trades"] > 0 else 0.0
    
    # Symbol performance
    symbol_performance: Dict[str, Dict] = {}
    for trade in user_trades:
        symbol = trade["symbol"]
        if symbol not in symbol_performance:
            symbol_performance[symbol] = {
                "trades": 0,
                "total_pnl": 0.0
            }
        symbol_performance[symbol]["trades"] += 1
        symbol_performance[symbol]["total_pnl"] += trade["pnl"]
    
    for symbol in symbol_performance:
        symbol_performance[symbol]["average_pnl"] = (
            symbol_performance[symbol]["total_pnl"] / symbol_performance[symbol]["trades"]
        )
    
    # Day of week analysis
    dow_performance: Dict[str, Dict] = {}
    for trade in user_trades:
        entry_date = datetime.fromisoformat(trade["entry_date"])
        day_of_week = entry_date.strftime("%A")
        if day_of_week not in dow_performance:
            dow_performance[day_of_week] = {
                "trades": 0,
                "total_pnl": 0.0
            }
        dow_performance[day_of_week]["trades"] += 1
        dow_performance[day_of_week]["total_pnl"] += trade["pnl"]
    
    for day in dow_performance:
        dow_performance[day]["average_pnl"] = (
            dow_performance[day]["total_pnl"] / dow_performance[day]["trades"]
        )
    
    return {
        "strategy_breakdown": strategy_performance,
        "symbol_performance": symbol_performance,
        "day_of_week_performance": dow_performance,
        "total_trades_analyzed": len(user_trades)
    }


@router.get("/export/{user_id}")
def export_trade_journal(user_id: str, format: str = "json"):
    """Export trade journal in various formats."""
    user_trades = [t for t in trade_journal.values() if t["user_id"] == int(user_id)]
    
    if format == "json":
        return {"trades": user_trades}
    elif format == "csv":
        import csv
        from io import StringIO
        
        output = StringIO()
        if user_trades:
            writer = csv.DictWriter(output, fieldnames=user_trades[0].keys())
            writer.writeheader()
            writer.writerows(user_trades)
        
        return {"csv": output.getvalue()}
    else:
        raise HTTPException(status_code=400, detail="Unsupported format")
