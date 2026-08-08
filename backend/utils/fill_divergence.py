"""
utils/fill_divergence.py — paper-vs-live fill-quality comparison for a
single custom strategy, from its own CLOSED CustomStrategyPosition rows
(db/models.py — mode='paper'|'live'). Answers "does this strategy actually
perform differently once real money/real broker latency is involved,"
QuantConnect's own recommended practice (diff live fills against
backtest/paper assumptions) adapted to what this schema can actually
support.

Deliberately NOT a tick-level price-slippage report — this codebase keeps
no LTP tick history for arbitrary instruments (only EOD bhavcopy), so
there is no real "expected price at order time" to diff a fill against
after the fact; inventing one would violate this codebase's own "never a
fabricated number" discipline (see utils/backtest_stats.py's docstring).
What IS real and stored: entry_price/exit_price/exit_reason/timestamps on
every closed leg — so this compares REALIZED P&L-per-unit, holding
duration, and exit-reason mix between the two modes instead.

Pure function over plain leg dicts — no DB import here — same
independently-testable discipline as utils/backtest_stats.py.
"""
from collections import Counter
from datetime import datetime

_MIN_LEGS_FOR_COMPARISON = 5


def _pnl_per_unit(leg: dict) -> float | None:
    """(exit - entry) for a long leg, (entry - exit) for a short — None if either price is missing (still-open leg slipped into the input by mistake)."""
    entry, exit_ = leg.get("entry_price"), leg.get("exit_price")
    if entry is None or exit_ is None:
        return None
    return (exit_ - entry) if leg.get("transaction_type") == "BUY" else (entry - exit_)


def _holding_hours(leg: dict) -> float | None:
    opened, closed = leg.get("opened_at"), leg.get("closed_at")
    if not opened or not closed:
        return None
    try:
        opened_dt = datetime.fromisoformat(opened)
        closed_dt = datetime.fromisoformat(closed)
    except (TypeError, ValueError):
        return None
    return (closed_dt - opened_dt).total_seconds() / 3600.0


def _summarize(legs: list[dict]) -> dict:
    closed = [leg for leg in legs if leg.get("status") == "CLOSED"]
    pnl_values = [v for v in (_pnl_per_unit(leg) for leg in closed) if v is not None]
    holding_values = [v for v in (_holding_hours(leg) for leg in closed) if v is not None]
    exit_reasons = Counter(leg.get("exit_reason") or "UNKNOWN" for leg in closed)

    return {
        "legs_closed": len(closed),
        "avg_pnl_per_unit": round(sum(pnl_values) / len(pnl_values), 4) if pnl_values else None,
        "avg_holding_hours": round(sum(holding_values) / len(holding_values), 2) if holding_values else None,
        "exit_reason_breakdown": dict(exit_reasons),
        "sample_size_warning": "very_limited" if len(closed) < _MIN_LEGS_FOR_COMPARISON else None,
    }


def compute_fill_divergence(paper_legs: list[dict], live_legs: list[dict]) -> dict:
    """
    Args:
        paper_legs, live_legs: CustomStrategyPosition.to_dict() rows for
            this strategy, already filtered to mode='paper'/'live'
            respectively (any status — closed-only filtering happens
            inside _summarize).

    Returns:
        paper, live: per-mode summaries (see _summarize).
        avg_pnl_per_unit_diff: live - paper (positive = live performing
            BETTER per unit than paper suggested; negative = live is
            underperforming its own paper track record — the signal worth
            watching).
        avg_holding_hours_diff: live - paper (a large positive value can
            indicate live exits lagging paper's, e.g. broker/network
            latency on exit orders).
        comparable: False (with both summaries still returned) if either
            side has fewer than 5 closed legs — too little live history
            to trust a comparison, same 'very_limited' threshold
            compute_backtest_stats() uses elsewhere in this codebase.
    """
    paper_summary = _summarize(paper_legs)
    live_summary = _summarize(live_legs)

    comparable = paper_summary["sample_size_warning"] is None and live_summary["sample_size_warning"] is None

    avg_pnl_diff = None
    if paper_summary["avg_pnl_per_unit"] is not None and live_summary["avg_pnl_per_unit"] is not None:
        avg_pnl_diff = round(live_summary["avg_pnl_per_unit"] - paper_summary["avg_pnl_per_unit"], 4)

    avg_holding_diff = None
    if paper_summary["avg_holding_hours"] is not None and live_summary["avg_holding_hours"] is not None:
        avg_holding_diff = round(live_summary["avg_holding_hours"] - paper_summary["avg_holding_hours"], 2)

    return {
        "paper": paper_summary,
        "live": live_summary,
        "avg_pnl_per_unit_diff": avg_pnl_diff,
        "avg_holding_hours_diff": avg_holding_diff,
        "comparable": comparable,
    }
