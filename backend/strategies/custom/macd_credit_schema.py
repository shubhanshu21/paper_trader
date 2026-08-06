"""
strategies/custom/macd_credit_schema.py — the rule contract for the
MACD-based overnight directional credit-spread "Stop & Reverse" strategy.
See macd_credit_strategy.py for the executor and
api/macd_credit_engine.py for the tick function (registered in
api/strategy_scheduler.py's dispatch — see engine_registry.py for the
matching route-layer registration).

An EIGHTH separate engine — none of the following has any equivalent in
the leg-based engine's static entry/exit model:
  - a "Stop & Reverse" system: there is ALWAYS an active directional
    position once the strategy has started (a hourly MACD/signal-line
    crossover doesn't just exit the current spread, it immediately opens
    the OPPOSITE one) — the leg-based engine's "exit" is always a full
    stop, never a same-tick re-entry in the reverse direction.
  - strike selection by searching for a (short, hedge) PAIR whose net
    entry CREDIT falls in a target ₹ band — not a fixed points/percent/
    premium-of-one-leg offset.
  - a rollover rule keyed off the CALENDAR DAY of the month a signal
    fires on (before/after the 15th selects this month's vs. next
    month's expiry), independent of expiry_offset's index-based meaning
    elsewhere in this app.

Rules JSON shape::

    {
      "lots": <int> >= 1,
      "macd_fast": <int> >= 1,           # default 12
      "macd_slow": <int> >= 1,           # default 26 (must be > macd_fast)
      "macd_signal": <int> >= 1,         # default 9
      "credit_min": <number> > 0,        # default 90 — target net credit (₹, per lot's
      "credit_max": <number> > 0,        #   worth of premium) band for the (short, hedge)
                                          #   strike pair search — default 140
      "credit_search_max_steps": <int> >= 1, # default 40 — safety cap on how far outward
                                          #   from spot the (short, hedge) search looks
                                          #   before giving up (never guesses a pair outside
                                          #   the configured credit band)
      "rollover_day_of_month": <int> 1-28, # default 15 — a signal firing ON OR BEFORE this
                                          #   day-of-month trades the CURRENT month's expiry;
                                          #   AFTER it trades NEXT month's
      "exit_days_before_expiry": <int> >= 0 # default 2 — hard stop, same meaning as
                                          #   everywhere else in this app; on this trigger the
                                          #   position is closed and NOT reversed — the system
                                          #   waits for the next real MACD signal on the new
                                          #   expiry instead of holding into rollover week
    }
"""

_DEFAULTS = {
    "macd_fast": 12,
    "macd_slow": 26,
    "macd_signal": 9,
    "credit_min": 90,
    "credit_max": 140,
    "credit_search_max_steps": 40,
    "rollover_day_of_month": 15,
    "exit_days_before_expiry": 2,
}


def get_setting(rules: dict, key: str):
    return rules.get(key, _DEFAULTS.get(key))


def validate_macd_credit_rules(rules: dict) -> list[str]:
    """Return a list of human-readable error strings; empty list = valid. Mirrors rule_schema.validate_rules()'s contract."""
    errors: list[str] = []
    if not isinstance(rules, dict):
        return ["Strategy rules must be an object."]

    lots = rules.get("lots", 1)
    if not isinstance(lots, int) or lots < 1:
        errors.append("'lots' must be a positive whole number.")

    for key in ("macd_fast", "macd_slow", "macd_signal", "credit_search_max_steps"):
        val = get_setting(rules, key)
        if not isinstance(val, int) or val < 1:
            errors.append(f"'{key}' must be a positive whole number.")

    fast, slow = get_setting(rules, "macd_fast"), get_setting(rules, "macd_slow")
    if isinstance(fast, int) and isinstance(slow, int) and fast >= slow:
        errors.append("'macd_fast' must be less than 'macd_slow'.")

    for key in ("credit_min", "credit_max"):
        val = get_setting(rules, key)
        if not isinstance(val, (int, float)) or val <= 0:
            errors.append(f"'{key}' must be a positive number.")
    credit_min, credit_max = get_setting(rules, "credit_min"), get_setting(rules, "credit_max")
    if isinstance(credit_min, (int, float)) and isinstance(credit_max, (int, float)) and credit_min > credit_max:
        errors.append("'credit_min' must not be greater than 'credit_max'.")

    rollover_day = get_setting(rules, "rollover_day_of_month")
    if not isinstance(rollover_day, int) or not (1 <= rollover_day <= 28):
        errors.append("'rollover_day_of_month' must be a whole number from 1 to 28.")

    exit_days = get_setting(rules, "exit_days_before_expiry")
    if not isinstance(exit_days, int) or exit_days < 0:
        errors.append("'exit_days_before_expiry' must be a whole number >= 0.")

    return errors


def describe_macd_credit_rules(rules: dict | None, symbol: str = "") -> str:
    """Plain-English summary — same role as rule_schema.describe_rules() for the leg-based shape."""
    if not rules:
        return "No rules configured yet."
    lots = rules.get("lots", 1)
    fast, slow, sig = get_setting(rules, "macd_fast"), get_setting(rules, "macd_slow"), get_setting(rules, "macd_signal")
    credit_min, credit_max = get_setting(rules, "credit_min"), get_setting(rules, "credit_max")
    rollover_day = get_setting(rules, "rollover_day_of_month")
    exit_days = get_setting(rules, "exit_days_before_expiry")
    subject = f"{symbol} " if symbol else ""
    return (
        f"SELL {lots} lot{'s' if lots != 1 else ''} {subject}monthly credit spread, Stop-&-Reverse on the hourly "
        f"MACD({fast},{slow},{sig}): MACD above signal -> Bull Put Spread, below -> Bear Call Spread, always one "
        f"or the other open, flipping the instant the signal crosses. Strikes searched for a net credit between "
        f"₹{credit_min} and ₹{credit_max}. A signal on/before day {rollover_day} of the month trades the current "
        f"month's expiry, after it trades next month's. Force-closes (without reversing) {exit_days} day(s) "
        f"before expiry — waits for the next real signal on the new expiry instead of holding into rollover week."
    )
