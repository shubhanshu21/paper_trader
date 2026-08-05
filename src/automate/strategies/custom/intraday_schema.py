"""
strategies/custom/intraday_schema.py — the rule contract for the
Supertrend + Pivot Point intraday option-selling strategy. See
strategies/custom/intraday_indicator_strategy.py for the executor and
api/intraday_indicator_scheduler.py for the scheduler that drives it.

Deliberately its OWN schema/executor/scheduler, not a variant of
rule_schema.py/RuleBasedStrategy/custom_strategy_scheduler.py — that
whole stack is built on "one fixed set of legs, entered once per expiry
cycle, held until TP/SL/time/expiry." This strategy breaks every one of
those assumptions: which option_type to sell is decided LIVE by a signal
(not fixed at build time), it can enter/exit several INDEPENDENT times
within a single day, and its exit trigger is "the indicator itself
flipped," not any of the existing %/₹/breakeven/time exit shapes.
Force-fitting it into rule_schema's leg model would corrupt that
model's real invariant instead of extending it.

A CustomStrategy row using this schema is marked by
`strategy_type == "SUPERTREND_INTRADAY"` (see db/models.py) — that value
is what routes custom_strategy_scheduler.py to SKIP it (see that
module's main query) and api/intraday_indicator_scheduler.py to pick it
up instead. instrument_type/symbols (exactly one symbol expected) still
live on the CustomStrategy row itself, same columns every other custom
strategy uses.

Rules JSON shape::

    {
      "lots": <int> >= 1,
      "supertrend_period": <int> >= 1,        # default 7 — Supertrend(7,3) per the reference strategy
      "supertrend_multiplier": <number> > 0,  # default 3
      "candle_interval_minutes": <int> >= 1,  # default 5
      "max_trades_per_day": <int> >= 1,       # default 3 — counts ENTRIES today (see scheduler), not open positions
      "exit_time": "HH:MM",                   # default "15:15" — square off everything by this time, no overnight positions
      "capital_buffer_per_lot": <number> > 0 | null  # informational only (shown in the UI/description) — this
    }                                                  #   engine doesn't gate entries on account balance beyond the
                                                        #   normal pre-trade margin/compliance checks every order
                                                        #   already goes through; it's a manual sizing guideline from
                                                        #   the reference strategy, not an automated hard stop.
"""

_DEFAULTS = {
    "supertrend_period": 7,
    "supertrend_multiplier": 3,
    "candle_interval_minutes": 5,
    "max_trades_per_day": 3,
    "exit_time": "15:15",
}


def _is_hhmm(value) -> bool:
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        return False
    h, m = value[:2], value[3:]
    return h.isdigit() and m.isdigit() and 0 <= int(h) <= 23 and 0 <= int(m) <= 59


def get_setting(rules: dict, key: str):
    """Every field except `lots`/`capital_buffer_per_lot` has a documented default — this is the one place that default is applied, so the executor/scheduler/description never have to repeat it."""
    return rules.get(key, _DEFAULTS.get(key))


def validate_intraday_rules(rules: dict) -> list[str]:
    """Return a list of human-readable error strings; empty list = valid. Mirrors rule_schema.validate_rules()'s contract exactly, just for this shape."""
    errors: list[str] = []
    if not isinstance(rules, dict):
        return ["Strategy rules must be an object."]

    lots = rules.get("lots", 1)
    if not isinstance(lots, int) or lots < 1:
        errors.append("'lots' must be a positive whole number.")

    period = get_setting(rules, "supertrend_period")
    if not isinstance(period, int) or period < 1:
        errors.append("'supertrend_period' must be a positive whole number.")

    multiplier = get_setting(rules, "supertrend_multiplier")
    if not isinstance(multiplier, (int, float)) or multiplier <= 0:
        errors.append("'supertrend_multiplier' must be a positive number.")

    interval = get_setting(rules, "candle_interval_minutes")
    if not isinstance(interval, int) or interval < 1:
        errors.append("'candle_interval_minutes' must be a positive whole number.")

    max_trades = get_setting(rules, "max_trades_per_day")
    if not isinstance(max_trades, int) or max_trades < 1:
        errors.append("'max_trades_per_day' must be a positive whole number.")

    exit_time = get_setting(rules, "exit_time")
    if not _is_hhmm(exit_time):
        errors.append("'exit_time' must be in HH:MM format.")

    buffer = rules.get("capital_buffer_per_lot")
    if buffer is not None and (not isinstance(buffer, (int, float)) or buffer <= 0):
        errors.append("'capital_buffer_per_lot' must be a positive number, or omitted.")

    return errors


def describe_intraday_rules(rules: dict | None, symbol: str = "") -> str:
    """Plain-English summary — same role as rule_schema.describe_rules() for the leg-based shape."""
    if not rules:
        return "No rules configured yet."
    period = get_setting(rules, "supertrend_period")
    multiplier = get_setting(rules, "supertrend_multiplier")
    interval = get_setting(rules, "candle_interval_minutes")
    lots = rules.get("lots", 1)
    max_trades = get_setting(rules, "max_trades_per_day")
    exit_time = get_setting(rules, "exit_time")
    subject = f"{symbol} " if symbol else ""
    return (
        f"SELL {lots} lot{'s' if lots != 1 else ''} ATM PUT on {subject}when a {interval}-min candle closes above "
        f"Supertrend({period},{multiplier}) and above pivot R1; SELL ATM CALL when it closes below Supertrend and "
        f"below pivot S1 — one position at a time. Exit on a Supertrend flip, {exit_time} time exit (no overnight "
        f"positions), or after {max_trades} trade{'s' if max_trades != 1 else ''} today, whichever comes first."
    )
