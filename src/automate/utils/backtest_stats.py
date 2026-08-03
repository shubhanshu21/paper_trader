"""
utils/backtest_stats.py — standard quant-backtest performance/risk metrics
computed from a CustomRuleBacktestEngine cycle list (see
backtest/custom_engine.py), on top of the plain win-rate/avg-return that
already existed. Everything here is derived purely from `cycles` (+ the
two small optional external inputs documented below) — no broker calls, no
DB access — so this stays a pure function, easy to unit-test and safe to
call on any already-computed cycle list, live or cached.

Indian-market-specific by design, not generic: the risk-free rate used for
Sharpe/Sortino is `black76.DEFAULT_RISK_FREE_RATE` (the same India rate
already used for options Greeks elsewhere in this codebase), and the
benchmark return callers are expected to pass in is NIFTY 50's buy-and-hold
return over the same span (see custom_engine.compute_nifty_benchmark_return),
not S&P 500 or any other index.
"""
import math
import statistics
from datetime import date

from automate.utils import black76

# Starting notional for the compounded equity curve — arbitrary (this
# engine has no real "account size" concept, cycles are sized in
# %-of-premium terms), but needed to turn per-cycle %-returns into a
# multiplicative curve that CAGR/Sharpe/Sortino/Calmar/drawdown-duration
# can be computed from meaningfully. Pick a round number so the curve
# itself is legible if a caller inspects it directly.
_NOTIONAL_START = 100_000.0


def _cycles_per_year(rules: dict | None) -> float:
    """
    How many entry cycles a year this strategy's expiry mode implies —
    needed to annualize Sharpe/Sortino (their per-cycle mean/stdev must be
    scaled by sqrt(periods_per_year), same as annualizing daily returns by
    sqrt(252) for a daily-bar strategy). WEEKLY/MONTHLY are the only two
    expiry modes this platform supports (see rule_schema.py) — default to
    MONTHLY (12) if rules are missing or the mode is unrecognized, since
    that's this platform's original/most common mode.
    """
    mode = ((rules or {}).get("expiry") or {}).get("mode", "MONTHLY")
    return 52.0 if mode == "WEEKLY" else 12.0


def _merge_intervals_total_days(intervals: list[tuple]) -> int:
    """Union of [start, end] date-interval day-spans, merging overlaps — used for exposure_pct so overlapping multi-symbol cycles aren't double-counted."""
    if not intervals:
        return 0
    ordered = sorted(intervals)
    merged: list[list] = [list(ordered[0])]
    for start, end in ordered[1:]:
        last = merged[-1]
        if start <= last[1]:
            last[1] = max(last[1], end)
        else:
            merged.append([start, end])
    return sum((e - s).days for s, e in merged)


def compute_backtest_stats(
    cycles: list[dict],
    rules: dict | None = None,
    benchmark_return_pct: float | None = None,
) -> dict:
    """
    Args:
        cycles: CustomRuleBacktestEngine.run()'s output, in chronological
            order (oldest entry_date first) — callers merging multiple
            symbols' cycles together MUST sort by entry_date first, or
            every metric below (equity curve, drawdown, streaks) will be
            wrong. Each cycle has net_pnl (₹), pnl_pct_of_premium (%),
            won (bool), entry_date/exit_date ('YYYY-MM-DD').
        rules: the strategy's rules dict (for expiry.mode — see
            _cycles_per_year) — used only to annualize Sharpe/Sortino.
        benchmark_return_pct: NIFTY 50's buy-and-hold %% return over the
            same [first entry_date, last exit_date] span (see
            custom_engine.compute_nifty_benchmark_return) — None if it
            couldn't be resolved; alpha_pct is then also None.

    Returns additional aggregate stats (existing fields kept for backward
    compat; see git history for their docstrings):
        total_net_pnl, max_drawdown_pct, profit_factor,
        max_consecutive_wins/losses, best/worst_cycle_pct,
        avg_win/loss_pct, equity_curve (additive, % — original shape).

    New fields:
        equity_curve_compounded: ₹ curve from a 100,000 notional,
            compounding each cycle's %-of-premium return — what CAGR/
            Sharpe/Sortino/Calmar/drawdown-duration are actually computed
            from (an additive %-sum curve can't support any of these
            correctly).
        total_return_pct: overall %% return of that compounded curve.
        cagr_pct: annualized return, compounded curve, calendar days.
        sharpe_ratio, sortino_ratio: annualized, excess over
            black76.DEFAULT_RISK_FREE_RATE (India rate), None if there's
            no variance to divide by or too few cycles.
        calmar_ratio: cagr_pct / max_drawdown_pct.
        max_drawdown_duration_days, max_drawdown_ongoing: longest
            peak-to-recovery span on the compounded curve; ongoing=True if
            the last cycle is still below its prior peak.
        exposure_pct: %% of the full date span with at least one position
            open (interval-union of entry->exit spans, so overlapping
            multi-symbol cycles aren't double-counted).
        benchmark_return_pct, alpha_pct: passed through / total_return_pct
            minus benchmark.
        sample_size_warning: 'very_limited' (<5 cycles), 'limited' (<20),
            else None — cheap, honest "don't over-trust this" signal
            rather than a hard block (matches TradingView's own low-trade-
            count warning practice).
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
            "equity_curve_compounded": [],
            "total_return_pct": None,
            "cagr_pct": None,
            "sharpe_ratio": None,
            "sortino_ratio": None,
            "calmar_ratio": None,
            "max_drawdown_duration_days": None,
            "max_drawdown_ongoing": False,
            "exposure_pct": None,
            "benchmark_return_pct": benchmark_return_pct,
            "alpha_pct": None,
            "sample_size_warning": "very_limited",
        }

    total_net_pnl = round(sum(c["net_pnl"] for c in cycles), 2)

    # Additive equity curve (original shape, kept for backward compat) +
    # compounded ₹ curve (what every new risk metric below uses) + max
    # drawdown-duration, walked together in one pass over the SAME
    # chronological order the caller guarantees.
    cumulative = 0.0
    equity_curve = []
    peak = 0.0
    max_drawdown_pct = 0.0

    capital = _NOTIONAL_START
    equity_curve_compounded = []
    peak_capital = _NOTIONAL_START
    peak_date = date.fromisoformat(cycles[0]["entry_date"])
    max_drawdown_duration_days = 0
    max_drawdown_ongoing = False

    for c in cycles:
        cumulative += c["pnl_pct_of_premium"]
        equity_curve.append(round(cumulative, 2))
        peak = max(peak, cumulative)
        max_drawdown_pct = max(max_drawdown_pct, peak - cumulative)

        capital *= (1.0 + c["pnl_pct_of_premium"] / 100.0)
        equity_curve_compounded.append(round(capital, 2))
        exit_dt = date.fromisoformat(c["exit_date"])
        if capital >= peak_capital:
            if max_drawdown_ongoing:
                # Recovery point — THIS is when "how long was I underwater"
                # is actually known (peak -> the date we finally clawed
                # back to/past it), not at the last still-underwater cycle.
                duration = (exit_dt - peak_date).days
                if duration > max_drawdown_duration_days:
                    max_drawdown_duration_days = duration
            peak_capital = capital
            peak_date = exit_dt
            max_drawdown_ongoing = False
        else:
            max_drawdown_ongoing = True

    if max_drawdown_ongoing:
        # Never recovered by the last cycle — duration so far, measured to
        # the last cycle's exit (the caller should treat this as a lower
        # bound, not "the" duration — see max_drawdown_ongoing).
        last_exit_dt = date.fromisoformat(cycles[-1]["exit_date"])
        duration = (last_exit_dt - peak_date).days
        if duration > max_drawdown_duration_days:
            max_drawdown_duration_days = duration

    total_return_pct = round((capital / _NOTIONAL_START - 1.0) * 100.0, 2)

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

    # --- CAGR (compounded curve, calendar days) ---
    first_entry = date.fromisoformat(cycles[0]["entry_date"])
    last_exit = max(date.fromisoformat(c["exit_date"]) for c in cycles)
    days_span = max((last_exit - first_entry).days, 1)
    cagr_pct = round(((capital / _NOTIONAL_START) ** (365.0 / days_span) - 1.0) * 100.0, 2) if capital > 0 else None

    # --- Sharpe / Sortino (annualized, excess over the India risk-free rate) ---
    per_cycle_returns = [c["pnl_pct_of_premium"] / 100.0 for c in cycles]
    n_per_year = _cycles_per_year(rules)
    risk_free_per_cycle = (1.0 + black76.DEFAULT_RISK_FREE_RATE) ** (1.0 / n_per_year) - 1.0
    excess_returns = [r - risk_free_per_cycle for r in per_cycle_returns]

    sharpe_ratio = None
    if len(excess_returns) >= 2:
        stdev = statistics.stdev(excess_returns)
        if stdev > 0:
            sharpe_ratio = round(statistics.mean(excess_returns) / stdev * math.sqrt(n_per_year), 2)

    sortino_ratio = None
    downside = [r for r in excess_returns if r < 0]
    if len(downside) >= 2:
        downside_stdev = statistics.stdev(downside)
        if downside_stdev > 0:
            sortino_ratio = round(statistics.mean(excess_returns) / downside_stdev * math.sqrt(n_per_year), 2)

    calmar_ratio = round(cagr_pct / max_drawdown_pct, 2) if (cagr_pct is not None and max_drawdown_pct > 0) else None

    # --- Exposure: %% of the full span with >=1 position open, union of
    # entry->exit intervals so overlapping multi-symbol cycles don't
    # double-count the same calendar day twice. ---
    intervals = [(date.fromisoformat(c["entry_date"]), date.fromisoformat(c["exit_date"])) for c in cycles]
    exposed_days = _merge_intervals_total_days(intervals)
    exposure_pct = round(min(exposed_days / days_span * 100.0, 100.0), 2)

    alpha_pct = round(total_return_pct - benchmark_return_pct, 2) if benchmark_return_pct is not None else None

    sample_size_warning = "very_limited" if len(cycles) < 5 else ("limited" if len(cycles) < 20 else None)

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
        "equity_curve_compounded": equity_curve_compounded,
        "total_return_pct": total_return_pct,
        "cagr_pct": cagr_pct,
        "sharpe_ratio": sharpe_ratio,
        "sortino_ratio": sortino_ratio,
        "calmar_ratio": calmar_ratio,
        "max_drawdown_duration_days": max_drawdown_duration_days,
        "max_drawdown_ongoing": max_drawdown_ongoing,
        "exposure_pct": exposure_pct,
        "benchmark_return_pct": round(benchmark_return_pct, 2) if benchmark_return_pct is not None else None,
        "alpha_pct": alpha_pct,
        "sample_size_warning": sample_size_warning,
    }
