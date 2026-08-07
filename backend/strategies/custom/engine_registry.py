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
from strategies.custom.delta_neutral_schema import (
    describe_delta_neutral_rules,
    validate_delta_neutral_rules,
)
from strategies.custom.gravity_schema import describe_gravity_rules, validate_gravity_rules
from strategies.custom.intraday_schema import describe_intraday_rules, validate_intraday_rules
from strategies.custom.macd_credit_schema import (
    describe_macd_credit_rules,
    validate_macd_credit_rules,
)
from strategies.custom.matrix_calendar_schema import (
    describe_matrix_calendar_rules,
    validate_matrix_calendar_rules,
)
from strategies.custom.nine_fifteen_schema import (
    describe_nine_fifteen_rules,
    validate_nine_fifteen_rules,
)
from strategies.custom.otm_put_roll_schema import (
    describe_otm_put_roll_rules,
    validate_otm_put_roll_rules,
)
from strategies.custom.rule_schema import describe_rules, validate_rules
from strategies.custom.session_seller_schema import (
    describe_session_seller_rules,
    validate_session_seller_rules,
)
from strategies.custom.smart_condor_schema import (
    describe_smart_condor_rules,
    validate_smart_condor_rules,
)
from strategies.custom.weekly_directional_schema import (
    describe_weekly_directional_rules,
    validate_weekly_directional_rules,
)
from strategies.custom.zero_to_hero_schema import (
    describe_zero_to_hero_rules,
    validate_zero_to_hero_rules,
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
    "SESSION_SELLER": {
        "validate": validate_session_seller_rules,
        "describe": describe_session_seller_rules,
        "backtest_supported": False,
        "backtest_unavailable_reason": (
            "Backtesting isn't available for this strategy yet — the backtest engine only has daily "
            "(end-of-day) historical data, but this strategy needs two intraday sessions a day of live "
            "option premiums. Paper trading works today and is the way to validate it for now."
        ),
    },
    "MACD_CREDIT_SPREAD": {
        "validate": validate_macd_credit_rules,
        "describe": describe_macd_credit_rules,
        "backtest_supported": False,
        "backtest_unavailable_reason": (
            "Backtesting isn't available for this strategy yet — the backtest engine only has daily "
            "(end-of-day) historical data, but this strategy's signal is read off hourly candles. "
            "Paper trading works today and is the way to validate it for now."
        ),
    },
    "DELTA_NEUTRAL_STRANGLE": {
        "validate": validate_delta_neutral_rules,
        "describe": describe_delta_neutral_rules,
        "backtest_supported": False,
        "backtest_unavailable_reason": (
            "Backtesting isn't available for this strategy yet — it needs a live-delta-driven, "
            "two-stage adjustment simulation (a premium-ratio rebalance and a separate delta-threshold "
            "full reset) the backtest engine doesn't have. Paper trading works today and is the way to "
            "validate it for now."
        ),
    },
    "WEEKLY_DIRECTIONAL": {
        "validate": validate_weekly_directional_rules,
        "describe": describe_weekly_directional_rules,
        "backtest_supported": False,
        "backtest_unavailable_reason": (
            "Backtesting isn't available for this strategy yet — the leg structure itself (which side "
            "gets 2x lots, which side gets the tail hedge) depends on a live EMA-crossover read plus a "
            "delta-targeted hedge search the backtest engine's fixed-leg cycle walk doesn't support. "
            "Paper trading works today and is the way to validate it for now."
        ),
    },
    "NINE_FIFTEEN_ORB": {
        "validate": validate_nine_fifteen_rules,
        "describe": describe_nine_fifteen_rules,
        "backtest_supported": False,
        "backtest_unavailable_reason": (
            "Backtesting isn't available for this strategy yet — it needs a live market-open scan across "
            "every F&O stock (today's #1 gainer/loser by real price move) the backtest engine's fixed-symbol, "
            "daily-candle simulation has no way to reproduce historically. Paper trading works today and is "
            "the way to validate it for now."
        ),
    },
    "ZERO_TO_HERO": {
        "validate": validate_zero_to_hero_rules,
        "describe": describe_zero_to_hero_rules,
        # True as of the synthetic intraday backtest engine
        # (backtest/synthetic_engine.py + backtest/synthetic_data_feed.py)
        # — routes_custom_strategies.py's backtest_strategy() dispatches
        # here automatically for this strategy_type (see
        # _SYNTHETIC_BACKTEST_RUNNER_NAMES), no manual choice needed.
        # IMPORTANT CAVEAT, surfaced in every result via "methodology_note"
        # (see _run_backtest_sync): option prices are Black-76
        # RECONSTRUCTIONS off real underlying candles (realized vol as an
        # IV proxy), not real historical option quotes — see that
        # module's own docstring for the full list of approximations.
        # Also NIFTY/BANKNIFTY only (db.models.Index1MinCandle's only
        # symbols) — any other symbol on this strategy_type still has
        # nothing to backtest against.
        "backtest_supported": True,
        "backtest_unavailable_reason": (
            "Backtesting isn't available for this strategy yet — it needs a live 15-minute candle-pattern "
            "read (a counter-color pullback run followed by a confirm candle) plus a price-level partial-"
            "booking simulation the backtest engine's daily-candle, fixed-leg cycle walk doesn't support. "
            "Paper trading works today and is the way to validate it for now."
        ),
    },
    "MATRIX_CALENDAR": {
        "validate": validate_matrix_calendar_rules,
        "describe": describe_matrix_calendar_rules,
        "backtest_supported": False,
        "backtest_unavailable_reason": (
            "Backtesting isn't available for this strategy yet — it needs a live delta-targeted strike "
            "search PLUS a simultaneous two-expiry (weekly + monthly) fill simulation the backtest "
            "engine's single-expiry cycle walk doesn't have. Paper trading works today and is the way "
            "to validate it for now."
        ),
    },
}


def get_engine(strategy_type: str | None) -> dict:
    """{"validate": fn(rules) -> list[str], "describe": fn(rules, symbol="") -> str, "backtest_supported": bool, "backtest_unavailable_reason": str | None} for `strategy_type`."""
    return _ENGINE_REGISTRY.get(strategy_type, _DEFAULT_ENGINE)
