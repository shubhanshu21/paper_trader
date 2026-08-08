"""
tests/test_fill_divergence.py — pure-function tests for
utils/fill_divergence.py's compute_fill_divergence(). Hand-built leg dicts
matching CustomStrategyPosition.to_dict()'s shape — no DB, no network.
"""
from utils.fill_divergence import compute_fill_divergence


def _leg(entry, exit_, transaction_type="SELL", status="CLOSED", exit_reason="TARGET",
         opened_at="2026-01-01T09:20:00", closed_at="2026-01-01T15:20:00"):
    return {
        "entry_price": entry, "exit_price": exit_, "transaction_type": transaction_type,
        "status": status, "exit_reason": exit_reason, "opened_at": opened_at, "closed_at": closed_at,
    }


class TestSummaryBasics:
    def test_short_leg_pnl_is_entry_minus_exit(self):
        legs = [_leg(100, 80, transaction_type="SELL")] * 5
        result = compute_fill_divergence(legs, [])
        assert result["paper"]["avg_pnl_per_unit"] == 20.0

    def test_long_leg_pnl_is_exit_minus_entry(self):
        legs = [_leg(100, 120, transaction_type="BUY")] * 5
        result = compute_fill_divergence(legs, [])
        assert result["paper"]["avg_pnl_per_unit"] == 20.0

    def test_open_legs_excluded_from_summary(self):
        legs = [_leg(100, 80)] * 5 + [_leg(100, None, status="OPEN")]
        result = compute_fill_divergence(legs, [])
        assert result["paper"]["legs_closed"] == 5

    def test_holding_hours_computed_from_timestamps(self):
        legs = [_leg(100, 80, opened_at="2026-01-01T09:00:00", closed_at="2026-01-01T15:00:00")] * 5
        result = compute_fill_divergence(legs, [])
        assert result["paper"]["avg_holding_hours"] == 6.0

    def test_exit_reason_breakdown_counts_correctly(self):
        legs = [_leg(100, 80, exit_reason="TARGET")] * 3 + [_leg(100, 90, exit_reason="STOP_LOSS")] * 2
        result = compute_fill_divergence(legs, [])
        assert result["paper"]["exit_reason_breakdown"] == {"TARGET": 3, "STOP_LOSS": 2}

    def test_missing_exit_reason_bucketed_unknown(self):
        legs = [_leg(100, 80, exit_reason=None)] * 5
        result = compute_fill_divergence(legs, [])
        assert result["paper"]["exit_reason_breakdown"] == {"UNKNOWN": 5}


class TestComparability:
    def test_both_sides_below_threshold_not_comparable(self):
        result = compute_fill_divergence([_leg(100, 80)] * 2, [_leg(100, 80)] * 2)
        assert result["comparable"] is False
        assert result["paper"]["sample_size_warning"] == "very_limited"
        assert result["live"]["sample_size_warning"] == "very_limited"

    def test_both_sides_at_or_above_threshold_comparable(self):
        result = compute_fill_divergence([_leg(100, 80)] * 5, [_leg(100, 80)] * 5)
        assert result["comparable"] is True

    def test_one_side_below_threshold_not_comparable(self):
        result = compute_fill_divergence([_leg(100, 80)] * 5, [_leg(100, 80)] * 2)
        assert result["comparable"] is False

    def test_empty_legs_never_crashes(self):
        result = compute_fill_divergence([], [])
        assert result["paper"]["avg_pnl_per_unit"] is None
        assert result["comparable"] is False


class TestDivergenceCalculation:
    def test_live_underperforming_paper_is_negative_diff(self):
        paper = [_leg(100, 80)] * 5  # +20/unit
        live = [_leg(100, 90)] * 5   # +10/unit
        result = compute_fill_divergence(paper, live)
        assert result["avg_pnl_per_unit_diff"] == -10.0

    def test_live_outperforming_paper_is_positive_diff(self):
        paper = [_leg(100, 90)] * 5  # +10/unit
        live = [_leg(100, 80)] * 5   # +20/unit
        result = compute_fill_divergence(paper, live)
        assert result["avg_pnl_per_unit_diff"] == 10.0

    def test_holding_hours_diff_computed(self):
        paper = [_leg(100, 80, opened_at="2026-01-01T09:00:00", closed_at="2026-01-01T11:00:00")] * 5  # 2h
        live = [_leg(100, 80, opened_at="2026-01-01T09:00:00", closed_at="2026-01-01T14:00:00")] * 5  # 5h
        result = compute_fill_divergence(paper, live)
        assert result["avg_holding_hours_diff"] == 3.0

    def test_diff_none_when_no_pnl_data(self):
        result = compute_fill_divergence([], [])
        assert result["avg_pnl_per_unit_diff"] is None
        assert result["avg_holding_hours_diff"] is None
