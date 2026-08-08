"""
utils/optimizer.py — grid-search parameter sweep over a custom strategy's
scalar exit knobs (stop_loss_pct, take_profit_pct today — the two knobs
every strategy has regardless of leg shape, unlike per-leg strike
parameters which vary strategy-to-strategy). Deliberately takes the
backtest itself as an injected `run_fn` — no broker/DB import here — so
this stays a pure, independently-testable function (same discipline as
utils/backtest_stats.py) and the caller decides how a rules dict actually
gets backtested (real CustomRuleBacktestEngine in production, a fake in
tests).

Not a genetic/Bayesian search — an exhaustive grid over the caller-supplied
candidate values, capped at MAX_COMBINATIONS so a request can't
accidentally trigger hundreds of sequential backtests. Ranked by Sharpe
ratio (None/undefined last, since "couldn't compute a Sharpe" is worse
information than a low-but-real one), tiebroken by total_return_pct.
"""
import copy
import itertools

from utils.backtest_stats import compute_backtest_stats

MAX_COMBINATIONS = 25
SUPPORTED_PARAMS = ("stop_loss_pct", "take_profit_pct")


def _apply_params(base_rules: dict, params: dict[str, float]) -> dict:
    """Deep-copies base_rules and overwrites rules['exit'][param] for each param in `params` — never mutates the caller's original rules dict (it's reused across every grid point)."""
    rules = copy.deepcopy(base_rules)
    exit_rule = rules.setdefault("exit", {})
    for key, value in params.items():
        exit_rule[key] = value
    return rules


def build_grid(param_grid: dict[str, list[float]]) -> list[dict[str, float]]:
    """
    Cartesian product of `param_grid` (e.g. {"stop_loss_pct": [10, 15],
    "take_profit_pct": [20, 30]} -> 4 combinations), each combination as a
    {param_name: value} dict in the order itertools.product yields them
    (stable, not sorted by any performance metric — build_grid doesn't run
    anything).

    Raises ValueError if a param name isn't in SUPPORTED_PARAMS, a value
    list is empty, or the resulting combination count exceeds
    MAX_COMBINATIONS — every one of these is a caller mistake worth
    surfacing immediately rather than silently truncating or guessing.
    """
    if not param_grid:
        raise ValueError("param_grid must have at least one parameter.")
    unsupported = set(param_grid) - set(SUPPORTED_PARAMS)
    if unsupported:
        raise ValueError(f"Unsupported optimizer parameter(s): {sorted(unsupported)}. Supported: {list(SUPPORTED_PARAMS)}.")
    for name, values in param_grid.items():
        if not values:
            raise ValueError(f"'{name}' has no candidate values.")

    names = list(param_grid.keys())
    value_lists = [param_grid[name] for name in names]
    total = 1
    for values in value_lists:
        total *= len(values)
    if total > MAX_COMBINATIONS:
        raise ValueError(f"Grid has {total} combinations, exceeding the cap of {MAX_COMBINATIONS} — narrow the parameter ranges.")

    return [dict(zip(names, combo, strict=True)) for combo in itertools.product(*value_lists)]


def grid_search(base_rules: dict, param_grid: dict[str, list[float]], run_fn) -> list[dict]:
    """
    Runs `run_fn(rules)` once per grid point (run_fn: rules dict -> cycles
    list, the same shape CustomRuleBacktestEngine.run()/_run_backtest_symbols
    return), scores each with compute_backtest_stats(), and returns every
    result ranked best-Sharpe-first.

    A grid point whose run_fn raises, or whose cycles list is empty
    (nothing tradable at those parameters over this date range), is
    included in the results with sharpe_ratio=None and an `error` field
    rather than silently dropped — the caller should see every parameter
    combination they asked for, including the ones that didn't work.
    """
    combinations = build_grid(param_grid)
    results = []
    for params in combinations:
        rules = _apply_params(base_rules, params)
        try:
            cycles = run_fn(rules)
        except Exception as exc:
            results.append({
                "params": params, "cycles_tested": 0, "win_rate_pct": None,
                "total_return_pct": None, "sharpe_ratio": None, "max_drawdown_pct": None,
                "error": str(exc),
            })
            continue

        if not cycles:
            results.append({
                "params": params, "cycles_tested": 0, "win_rate_pct": None,
                "total_return_pct": None, "sharpe_ratio": None, "max_drawdown_pct": None,
                "error": "No historical cycles produced a valid simulated trade at these parameters.",
            })
            continue

        stats = compute_backtest_stats(cycles)
        wins = sum(1 for c in cycles if c["won"])
        results.append({
            "params": params,
            "cycles_tested": len(cycles),
            "win_rate_pct": round(wins / len(cycles) * 100.0, 2),
            "total_return_pct": stats["total_return_pct"],
            "sharpe_ratio": stats["sharpe_ratio"],
            "max_drawdown_pct": stats["max_drawdown_pct"],
        })

    def _sort_key(r: dict):
        # None Sharpe sorts last regardless of sign; among real Sharpes,
        # higher is better; ties broken by total_return_pct (also None-last).
        sharpe = r["sharpe_ratio"]
        total_return = r["total_return_pct"]
        return (sharpe is None, -(sharpe or 0), total_return is None, -(total_return or 0))

    results.sort(key=_sort_key)
    return results
