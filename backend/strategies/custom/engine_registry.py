"""
strategies/custom/engine_registry.py — one small dispatch table for the
per-strategy-type differences routes_custom_strategies.py needs (rules
validation, plain-English description, and whether backtesting is even
possible for this shape), instead of repeating an
if/elif "SUPERTREND_INTRADAY"/elif "WEEKEND_GAP_COMBO"/else chain at every
call site.

CUSTOM, every legacy strategy_type (STRADDLE/STRANGLE/IRON_CONDOR/
BUTTERFLY), and any unknown/None value all fall through to the leg-based
default (rule_schema.py) — that's deliberate: it's what lets every future
strategy that's just a new leg combination land on the existing general
engine with ZERO new registry entries, and reserves an entry here for
genuinely new execution models only (see intraday_schema.py/
combo_schema.py's own module docstrings for why those two needed one).

See api/strategy_scheduler.py for the matching runtime-side dispatch
(strategy_type -> _tick_one_strategy implementation) — a separate,
smaller table since the scheduler only ever needs to route TWO of these
three fields' worth of behavior (entry/exit), not description/backtest.
"""
from strategies.custom.combo_schema import describe_combo_rules, validate_combo_rules
from strategies.custom.gravity_schema import describe_gravity_rules, validate_gravity_rules
from strategies.custom.intraday_schema import describe_intraday_rules, validate_intraday_rules
from strategies.custom.otm_put_roll_schema import (
    describe_otm_put_roll_rules,
    validate_otm_put_roll_rules,
)
from strategies.custom.rule_schema import describe_rules, validate_rules
from strategies.custom.smart_condor_schema import (
    describe_smart_condor_rules,
    validate_smart_condor_rules,
)

_DEFAULT_ENGINE = {
    "validate": validate_rules,
    "describe": describe_rules,
    "backtest_supported": True,
    "backtest_unavailable_reason": None,
}

_ENGINE_REGISTRY: dict[str, dict] = {
    "SUPERTREND_INTRADAY": {
        "validate": validate_intraday_rules,
        "describe": describe_intraday_rules,
        "backtest_supported": False,
        "backtest_unavailable_reason": (
            "Backtesting isn't available for this strategy yet — the backtest engine only has daily "
            "(end-of-day) historical data, but this strategy trades off 5-minute candles intraday. "
            "Paper trading works today and is the way to validate it for now."
        ),
    },
    "WEEKEND_GAP_COMBO": {
        "validate": validate_combo_rules,
        "describe": describe_combo_rules,
        "backtest_supported": False,
        "backtest_unavailable_reason": (
            "Backtesting isn't available for this strategy yet — it needs a combined Nifty+Sensex "
            "simulation the backtest engine doesn't have (single-symbol, one-cycle-per-expiry only). "
            "Paper trading works today and is the way to validate it for now."
        ),
    },
    "OTM_PUT_ROLL": {
        "validate": validate_otm_put_roll_rules,
        "describe": describe_otm_put_roll_rules,
        "backtest_supported": False,
        "backtest_unavailable_reason": (
            "Backtesting isn't available for this strategy yet — it needs a multi-roll simulation "
            "(replacing one leg with another mid-cycle, potentially several times) the backtest engine "
            "doesn't have. Paper trading works today and is the way to validate it for now."
        ),
    },
    "SMART_CONDOR": {
        "validate": validate_smart_condor_rules,
        "describe": describe_smart_condor_rules,
        "backtest_supported": False,
        "backtest_unavailable_reason": (
            "Backtesting isn't available for this strategy yet — it needs a live cross-leg premium-ratio "
            "simulation (swapping a side's short+hedge legs mid-cycle, up to twice) the backtest engine "
            "doesn't have. Paper trading works today and is the way to validate it for now."
        ),
    },
    "GRAVITY": {
        "validate": validate_gravity_rules,
        "describe": describe_gravity_rules,
        "backtest_supported": False,
        "backtest_unavailable_reason": (
            "Backtesting isn't available for this strategy yet — it needs a stateful daily-close signal "
            "(a Camarilla level breach, then a later close back inside it) plus a live-tracked strike "
            "extreme the backtest engine doesn't simulate. Paper trading works today and is the way to "
            "validate it for now."
        ),
    },
}


def get_engine(strategy_type: str | None) -> dict:
    """{"validate": fn(rules) -> list[str], "describe": fn(rules, symbol="") -> str, "backtest_supported": bool, "backtest_unavailable_reason": str | None} for `strategy_type`."""
    return _ENGINE_REGISTRY.get(strategy_type, _DEFAULT_ENGINE)
