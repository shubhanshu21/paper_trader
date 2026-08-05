"""
strategies/registry.py — Single source of truth for available
hand-written strategies.

Empty now: the two strategies that used to live here (ten_percent_otm_strangle,
equity_ma_crossover) were retired in favor of the generic custom-strategy
builder (strategies/custom/rule_strategy.py, driven by rows in the
custom_strategies table via api/custom_strategy_scheduler.py) — any
strategy those two covered can be recreated there instead. Kept as an
empty dict rather than deleted outright because backtest/engine.py,
backtest/historical_engine.py, cli/run_strategy.py, cli/run_daemon.py,
and cli/run_position_monitor.py still import STRATEGIES from here for
the (now-unused) hand-written-strategy path; add a new entry here only
if you write another hand-written strategy class outside the builder.
"""
STRATEGIES = {}
