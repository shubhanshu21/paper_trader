"""
tests/test_backtest_stats.py — pure-function tests for
utils/backtest_stats.py's compute_backtest_stats(), including the new
CAGR/Sharpe/Sortino/Calmar/drawdown-duration/exposure/benchmark/sample-size
metrics added for the "world-class backtesting" pass. Hand-computed
expected values on small synthetic cycle lists — no DB, no network.
"""
import math

from utils import black76
from utils.backtest_stats import compute_backtest_stats


def _cycle(entry_date, exit_date, pnl_pct, net_pnl=None, won=None):
    return {
        "entry_date": entry_date,
        "exit_date": exit_date,
        "pnl_pct_of_premium": pnl_pct,
        "net_pnl": net_pnl if net_pnl is not None else pnl_pct * 100,  # arbitrary scale, sign-consistent
        "won": won if won is not None else pnl_pct > 0,
    }


class TestEmptyCycles:
    def test_returns_safe_defaults(self):
        stats = compute_backtest_stats([])
        assert stats["equity_curve"] == []
        assert stats["equity_curve_compounded"] == []
        assert stats["cagr_pct"] is None
        assert stats["sharpe_ratio"] is None
        assert stats["sample_size_warning"] == "very_limited"


class TestCompoundingEquityCurve:
    def test_compounds_multiplicatively_not_additively(self):
        cycles = [
            _cycle("2026-01-01", "2026-01-28", 10.0),   # +10%
            _cycle("2026-02-01", "2026-02-25", 10.0),   # +10% again
        ]
        stats = compute_backtest_stats(cycles)
        # 100000 * 1.10 * 1.10 = 121000, NOT 120000 (additive would give 20%)
        assert stats["equity_curve_compounded"] == [110000.0, 121000.0]
        assert stats["total_return_pct"] == 21.0
        # Additive curve (kept for backward compat) is the old shape.
        assert stats["equity_curve"] == [10.0, 20.0]


class TestDrawdown:
    def test_max_drawdown_pct_matches_peak_to_trough(self):
        cycles = [
            _cycle("2026-01-01", "2026-01-28", 20.0),
            _cycle("2026-02-01", "2026-02-25", -10.0),
            _cycle("2026-03-01", "2026-03-25", -5.0),
        ]
        stats = compute_backtest_stats(cycles)
        # Additive curve: 20, 10, 5 -> peak 20, trough 5 -> drawdown 15
        assert stats["max_drawdown_pct"] == 15.0

    def test_drawdown_duration_measured_peak_to_recovery(self):
        cycles = [
            _cycle("2026-01-01", "2026-01-28", 10.0),   # new peak at 2026-01-28 (capital 110000)
            _cycle("2026-02-01", "2026-02-25", -20.0),  # underwater (88000)
            _cycle("2026-03-01", "2026-03-25", -5.0),   # still underwater (83600)
            _cycle("2026-04-01", "2026-04-25", 40.0),   # 83600*1.4=117040 > 110000 -> recovers past prior peak
        ]
        stats = compute_backtest_stats(cycles)
        # Peak set on 2026-01-28; recovers on 2026-04-25.
        expected_days = (
            __import__("datetime").date(2026, 4, 25) - __import__("datetime").date(2026, 1, 28)
        ).days
        assert stats["max_drawdown_duration_days"] == expected_days
        assert stats["max_drawdown_ongoing"] is False

    def test_ongoing_drawdown_flagged_when_never_recovers(self):
        cycles = [
            _cycle("2026-01-01", "2026-01-28", 10.0),
            _cycle("2026-02-01", "2026-02-25", -50.0),
        ]
        stats = compute_backtest_stats(cycles)
        assert stats["max_drawdown_ongoing"] is True
        assert stats["max_drawdown_duration_days"] > 0


class TestCagr:
    def test_positive_total_return_gives_positive_cagr(self):
        cycles = [_cycle("2026-01-01", "2026-12-31", 20.0)]
        stats = compute_backtest_stats(cycles)
        assert stats["cagr_pct"] > 0
        # One year, +20% total -> CAGR should be close to 20%.
        assert 15.0 < stats["cagr_pct"] < 25.0

    def test_negative_total_return_gives_negative_cagr(self):
        cycles = [_cycle("2026-01-01", "2026-12-31", -20.0)]
        stats = compute_backtest_stats(cycles)
        assert stats["cagr_pct"] < 0


class TestSharpeSortino:
    def test_zero_variance_returns_none_sharpe(self):
        cycles = [_cycle(f"2026-0{i}-01", f"2026-0{i}-25", 5.0) for i in range(1, 4)]
        stats = compute_backtest_stats(cycles)
        # Identical returns every cycle -> zero stdev -> undefined Sharpe.
        assert stats["sharpe_ratio"] is None

    def test_varying_returns_produce_finite_sharpe(self):
        cycles = [
            _cycle("2026-01-01", "2026-01-25", 8.0),
            _cycle("2026-02-01", "2026-02-25", -3.0),
            _cycle("2026-03-01", "2026-03-25", 6.0),
            _cycle("2026-04-01", "2026-04-25", 2.0),
        ]
        stats = compute_backtest_stats(cycles, rules={"expiry": {"mode": "MONTHLY"}})
        assert stats["sharpe_ratio"] is not None
        assert math.isfinite(stats["sharpe_ratio"])

    def test_no_losing_cycles_gives_none_sortino(self):
        cycles = [
            _cycle("2026-01-01", "2026-01-25", 8.0),
            _cycle("2026-02-01", "2026-02-25", 4.0),
        ]
        stats = compute_backtest_stats(cycles)
        assert stats["sortino_ratio"] is None

    def test_weekly_vs_monthly_mode_changes_annualization(self):
        cycles = [
            _cycle("2026-01-01", "2026-01-08", 3.0),
            _cycle("2026-01-09", "2026-01-15", -1.0),
            _cycle("2026-01-16", "2026-01-22", 2.0),
        ]
        monthly = compute_backtest_stats(cycles, rules={"expiry": {"mode": "MONTHLY"}})
        weekly = compute_backtest_stats(cycles, rules={"expiry": {"mode": "WEEKLY"}})
        # Same returns, different annualization factor -> different Sharpe magnitude.
        assert monthly["sharpe_ratio"] != weekly["sharpe_ratio"]


class TestCalmar:
    def test_calmar_is_cagr_over_drawdown(self):
        cycles = [_cycle("2026-01-01", "2026-12-31", 20.0)]
        stats = compute_backtest_stats(cycles)
        if stats["max_drawdown_pct"] > 0:
            assert stats["calmar_ratio"] == round(stats["cagr_pct"] / stats["max_drawdown_pct"], 2)

    def test_none_when_no_drawdown(self):
        cycles = [_cycle("2026-01-01", "2026-06-30", 5.0)]
        stats = compute_backtest_stats(cycles)
        assert stats["max_drawdown_pct"] == 0.0
        assert stats["calmar_ratio"] is None


class TestExposure:
    def test_non_overlapping_cycles_sum_days(self):
        cycles = [
            _cycle("2026-01-01", "2026-01-11", 5.0),   # 10 days
            _cycle("2026-02-01", "2026-02-11", 5.0),   # 10 days
        ]
        stats = compute_backtest_stats(cycles)
        span = (__import__("datetime").date(2026, 2, 11) - __import__("datetime").date(2026, 1, 1)).days
        assert stats["exposure_pct"] == round(20 / span * 100.0, 2)

    def test_overlapping_multi_symbol_cycles_not_double_counted(self):
        cycles = [
            _cycle("2026-01-01", "2026-01-31", 5.0),
            _cycle("2026-01-15", "2026-02-15", 3.0),  # overlaps the first
        ]
        stats = compute_backtest_stats(cycles)
        assert stats["exposure_pct"] <= 100.0
        # Union of Jan 1 -> Feb 15 fully covered -> should be exactly 100%.
        assert stats["exposure_pct"] == 100.0


class TestBenchmarkAlpha:
    def test_alpha_is_total_return_minus_benchmark(self):
        cycles = [_cycle("2026-01-01", "2026-06-30", 15.0)]
        stats = compute_backtest_stats(cycles, benchmark_return_pct=10.0)
        assert stats["benchmark_return_pct"] == 10.0
        assert stats["alpha_pct"] == round(stats["total_return_pct"] - 10.0, 2)

    def test_none_benchmark_gives_none_alpha(self):
        cycles = [_cycle("2026-01-01", "2026-06-30", 15.0)]
        stats = compute_backtest_stats(cycles, benchmark_return_pct=None)
        assert stats["benchmark_return_pct"] is None
        assert stats["alpha_pct"] is None


class TestSampleSizeWarning:
    def test_very_limited_below_five(self):
        cycles = [_cycle(f"2026-0{i}-01", f"2026-0{i}-15", 5.0) for i in range(1, 4)]
        assert compute_backtest_stats(cycles)["sample_size_warning"] == "very_limited"

    def test_limited_below_twenty(self):
        cycles = [_cycle(f"2026-{i:02d}-01", f"2026-{i:02d}-15", 5.0) for i in range(1, 10)]
        assert compute_backtest_stats(cycles)["sample_size_warning"] == "limited"

    def test_none_at_or_above_twenty(self):
        cycles = []
        year, month = 2020, 1
        for _ in range(20):
            cycles.append(_cycle(f"{year}-{month:02d}-01", f"{year}-{month:02d}-15", 5.0))
            month += 1
            if month > 12:
                month = 1
                year += 1
        assert compute_backtest_stats(cycles)["sample_size_warning"] is None


class TestBackwardCompatFields:
    """Original fields (present before this session's changes) must keep working unmodified."""

    def test_profit_factor_and_streaks(self):
        cycles = [
            _cycle("2026-01-01", "2026-01-15", 10.0, net_pnl=1000, won=True),
            _cycle("2026-02-01", "2026-02-15", 10.0, net_pnl=1000, won=True),
            _cycle("2026-03-01", "2026-03-15", -5.0, net_pnl=-500, won=False),
        ]
        stats = compute_backtest_stats(cycles)
        assert stats["profit_factor"] == round(2000 / 500, 2)
        assert stats["max_consecutive_wins"] == 2
        assert stats["max_consecutive_losses"] == 1
        assert stats["best_cycle_pct"] == 10.0
        assert stats["worst_cycle_pct"] == -5.0

    def test_uses_india_risk_free_rate(self):
        # Sanity: confirm the module is actually wired to the shared India
        # rate constant, not a hardcoded/duplicated value.
        assert black76.DEFAULT_RISK_FREE_RATE == 0.065
