"""
tests/test_optimizer.py — pure-function tests for utils/optimizer.py's
build_grid() and grid_search(). run_fn is a fake (rules -> cycles) so
these never touch a broker, DB, or the real backtest engine.
"""
import pytest

from utils.optimizer import MAX_COMBINATIONS, build_grid, grid_search


def _cycle(pnl_pct, won=None):
    return {
        "entry_date": "2026-01-01", "exit_date": "2026-01-15",
        "pnl_pct_of_premium": pnl_pct, "net_pnl": pnl_pct * 100,
        "won": won if won is not None else pnl_pct > 0,
    }


class TestBuildGrid:
    def test_single_param_produces_one_combo_per_value(self):
        grid = build_grid({"stop_loss_pct": [10, 15, 20]})
        assert grid == [{"stop_loss_pct": 10}, {"stop_loss_pct": 15}, {"stop_loss_pct": 20}]

    def test_two_params_produce_cartesian_product(self):
        grid = build_grid({"stop_loss_pct": [10, 20], "take_profit_pct": [30, 40]})
        assert len(grid) == 4
        assert {"stop_loss_pct": 10, "take_profit_pct": 30} in grid
        assert {"stop_loss_pct": 20, "take_profit_pct": 40} in grid

    def test_empty_grid_rejected(self):
        with pytest.raises(ValueError, match="at least one parameter"):
            build_grid({})

    def test_unsupported_param_rejected(self):
        with pytest.raises(ValueError, match="Unsupported"):
            build_grid({"strike_offset": [1, 2]})

    def test_empty_value_list_rejected(self):
        with pytest.raises(ValueError, match="no candidate values"):
            build_grid({"stop_loss_pct": []})

    def test_exceeding_cap_rejected(self):
        with pytest.raises(ValueError, match="exceeding the cap"):
            build_grid({"stop_loss_pct": list(range(10)), "take_profit_pct": list(range(10))})

    def test_exactly_at_cap_accepted(self):
        n = MAX_COMBINATIONS
        grid = build_grid({"stop_loss_pct": list(range(n))})
        assert len(grid) == n


class TestGridSearch:
    def test_ranks_by_sharpe_best_first(self):
        def run_fn(rules):
            sl = rules["exit"]["stop_loss_pct"]
            # Lower stop_loss_pct -> tighter risk -> better (fake) Sharpe in this stub.
            if sl == 10:
                return [_cycle(5.0), _cycle(4.0), _cycle(6.0), _cycle(5.0)]
            return [_cycle(5.0), _cycle(-8.0), _cycle(3.0), _cycle(-2.0)]

        results = grid_search({"legs": []}, {"stop_loss_pct": [10, 30]}, run_fn)
        assert len(results) == 2
        assert results[0]["params"] == {"stop_loss_pct": 10}
        assert results[0]["sharpe_ratio"] >= results[1]["sharpe_ratio"]

    def test_empty_cycles_included_with_error_not_dropped(self):
        def run_fn(rules):
            return []
        results = grid_search({"legs": []}, {"stop_loss_pct": [10, 20]}, run_fn)
        assert len(results) == 2
        assert all(r["sharpe_ratio"] is None and "error" in r for r in results)

    def test_run_fn_exception_captured_not_raised(self):
        def run_fn(rules):
            raise RuntimeError("no instrument key")
        results = grid_search({"legs": []}, {"stop_loss_pct": [10]}, run_fn)
        assert len(results) == 1
        assert results[0]["error"] == "no instrument key"

    def test_does_not_mutate_base_rules(self):
        base = {"legs": [], "exit": {"stop_loss_pct": 99, "take_profit_pct": 99}}

        def run_fn(rules):
            assert rules["exit"]["stop_loss_pct"] != 99  # confirms the copy was actually mutated, not the original
            return [_cycle(5.0)]

        grid_search(base, {"stop_loss_pct": [10, 20]}, run_fn)
        assert base["exit"]["stop_loss_pct"] == 99  # original untouched

    def test_both_params_applied_together(self):
        seen = []

        def run_fn(rules):
            seen.append((rules["exit"]["stop_loss_pct"], rules["exit"]["take_profit_pct"]))
            return [_cycle(1.0)]

        grid_search({"legs": []}, {"stop_loss_pct": [10, 20], "take_profit_pct": [30]}, run_fn)
        assert sorted(seen) == [(10, 30), (20, 30)]

    def test_result_count_matches_grid_size(self):
        def run_fn(rules):
            return [_cycle(1.0)]
        results = grid_search({"legs": []}, {"stop_loss_pct": [5, 10, 15], "take_profit_pct": [20, 30]}, run_fn)
        assert len(results) == 6
