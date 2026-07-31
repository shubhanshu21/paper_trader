"""
utils/backtest_stats.py — standard quant-backtest performance/risk metrics
computed from a CustomRuleBacktestEngine cycle list (see
backtest/custom_engine.py), on top of the plain win-rate/avg-return that
already existed. Every metric here is derived purely from `cycles` — no
extra data source, no extra broker calls — so this can run on any already-
computed cycle list, live or cached.
"""
from typing import List


def compute_backtest_stats(cycles: List[dict]) -> dict:
    """
    Args:
        cycles: CustomRuleBacktestEngine.run()'s output — each cycle has
            net_pnl (₹), pnl_pct_of_premium (%), won (bool).

    Returns additional aggregate stats:
        total_net_pnl: sum of all cycles' net_pnl, in ₹.
        max_drawdown_pct: largest peak-to-trough decline in the cumulative
            %-of-premium equity curve — the standard "how bad could it
            have gotten" risk figure, not visible from win rate alone.
        profit_factor: gross profit / gross loss (>1 means the strategy's
            winners outweigh its losers; industry-standard summary stat).
        max_consecutive_wins / max_consecutive_losses: longest streaks, in
            cycle order — a losing streak longer than someone's risk
            tolerance is exactly the kind of thing an average-return
            number hides.
        best_cycle_pct / worst_cycle_pct: single best/worst cycle by
            pnl_pct_of_premium.
        avg_win_pct / avg_loss_pct: average %-of-premium return, split by
            outcome — lets you see "wins are small, losses are big" (or
            the reverse) even when the average blends them into something
            deceptively fine-looking.
        equity_curve: cumulative pnl_pct_of_premium after each cycle, in
            chronological order — what a backtest report's line chart
            plots; the frontend renders this directly.
    """
    if not cycles:
        return {
            "total_net_pnl": 0.0,
            "max_drawdown_pct": 0.0,
            "profit_factor": None,
            "max_consecutive_wins": 0,
            "max_consecutive_losses": 0,
            "best_cycle_pct": None,
            "worst_cycle_pct": None,
            "avg_win_pct": None,
            "avg_loss_pct": None,
            "equity_curve": [],
        }

    total_net_pnl = round(sum(c["net_pnl"] for c in cycles), 2)

    # Equity curve + max drawdown, walked in the SAME chronological order
    # discover_cycles() already produces (oldest expiry first).
    cumulative = 0.0
    equity_curve = []
    peak = 0.0
    max_drawdown_pct = 0.0
    for c in cycles:
        cumulative += c["pnl_pct_of_premium"]
        equity_curve.append(round(cumulative, 2))
        peak = max(peak, cumulative)
        max_drawdown_pct = max(max_drawdown_pct, peak - cumulative)

    wins = [c["pnl_pct_of_premium"] for c in cycles if c["won"]]
    losses = [c["pnl_pct_of_premium"] for c in cycles if not c["won"]]
    gross_profit = sum(c["net_pnl"] for c in cycles if c["won"])
    gross_loss = abs(sum(c["net_pnl"] for c in cycles if not c["won"]))
    # None (not Infinity — not valid JSON) when there are no losing cycles
    # to divide by; the frontend renders that case as "No losses" instead.
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else None

    max_consecutive_wins = max_consecutive_losses = 0
    cur_wins = cur_losses = 0
    for c in cycles:
        if c["won"]:
            cur_wins += 1
            cur_losses = 0
        else:
            cur_losses += 1
            cur_wins = 0
        max_consecutive_wins = max(max_consecutive_wins, cur_wins)
        max_consecutive_losses = max(max_consecutive_losses, cur_losses)

    all_pct = [c["pnl_pct_of_premium"] for c in cycles]

    return {
        "total_net_pnl": total_net_pnl,
        "max_drawdown_pct": round(max_drawdown_pct, 2),
        "profit_factor": profit_factor,
        "max_consecutive_wins": max_consecutive_wins,
        "max_consecutive_losses": max_consecutive_losses,
        "best_cycle_pct": round(max(all_pct), 2),
        "worst_cycle_pct": round(min(all_pct), 2),
        "avg_win_pct": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss_pct": round(sum(losses) / len(losses), 2) if losses else None,
        "equity_curve": equity_curve,
    }
