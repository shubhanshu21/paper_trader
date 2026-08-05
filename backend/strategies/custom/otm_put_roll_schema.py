"""
strategies/custom/otm_put_roll_schema.py — the rule contract for the
far-month OTM Nifty put-selling strategy with a mechanical roll-down
adjustment protocol. See otm_put_roll_strategy.py for the executor and
api/otm_put_roll_engine.py for the tick function (registered in
api/strategy_scheduler.py's dispatch — see engine_registry.py for the
matching route-layer registration).

A FOURTH separate engine, not a variant of rule_schema.py — it breaks the
general engine's "enter once, exit once" model in a way none of the other
three engines do: this one can REPLACE its single open leg with a new one
at a different strike, several times, WITHIN the same still-open expiry
cycle (a "roll"), rather than ever fully exiting and re-entering. The
general engine has no concept of "close this leg but the cycle is still
running" — only "exit the whole cycle."

Rules JSON shape::

    {
      "lots": <int> >= 1,
      "initial_otm_points": <int> >= 1,     # default 500 — how far below spot the FIRST sold put is
      "expiry_offset": <int> >= 0,          # default 1 — 0 = nearest monthly, 1 = the one AFTER
                                             #   that ("far month" — see find_monthly_expiries)
      "pullback_lookback_days": <int> >= 1, # default 20 — trading days of daily closes used to
                                             #   find the "recent high" the pullback is measured from
      "pullback_min_points": <number> > 0,  # default 500 — minimum drop from that high required
                                             #   before entering AT ALL (never sell puts at/near a high)
      "roll_points": <int> >= 1,            # default 200 — how far down each roll moves the strike
      "max_rolls_per_cycle": <int> >= 0,    # default 8 — safety cap; once hit, the position is just
                                             #   held (no more automatic rolls) and a notification fires
                                             #   for manual review — never rolls without bound into a
                                             #   real crash
      "candle_interval_minutes": <int> >= 1,# default 60 — the pullback-entry and roll-trigger checks
                                             #   both evaluate off the latest CLOSED candle's close at
                                             #   this granularity, not a raw LTP tick, so a brief
                                             #   intraday wick that immediately reverts can't fire a
                                             #   false entry/roll
      "target_capital_pct": <number> > 0,   # default 5 — close everything once this cycle's
                                             #   CUMULATIVE P&L (every leg entered/rolled/closed so
                                             #   far this cycle, realized + the current open leg's
                                             #   unrealized) reaches this % of the real broker margin
                                             #   captured at the cycle's first entry
      "exit_days_before_expiry": <int> >= 0 # default 1 — hard stop, same meaning as everywhere else
                                             #   in this app; fires regardless of target/roll state
    }
"""

_DEFAULTS = {
    "initial_otm_points": 500,
    "expiry_offset": 1,
    "pullback_lookback_days": 20,
    "pullback_min_points": 500,
    "roll_points": 200,
    "max_rolls_per_cycle": 8,
    "candle_interval_minutes": 60,
    "target_capital_pct": 5,
    "exit_days_before_expiry": 1,
}


def get_setting(rules: dict, key: str):
    return rules.get(key, _DEFAULTS.get(key))


def validate_otm_put_roll_rules(rules: dict) -> list[str]:
    """Return a list of human-readable error strings; empty list = valid. Mirrors rule_schema.validate_rules()'s contract."""
    errors: list[str] = []
    if not isinstance(rules, dict):
        return ["Strategy rules must be an object."]

    lots = rules.get("lots", 1)
    if not isinstance(lots, int) or lots < 1:
        errors.append("'lots' must be a positive whole number.")

    for key in ("initial_otm_points", "pullback_lookback_days", "roll_points", "candle_interval_minutes"):
        val = get_setting(rules, key)
        if not isinstance(val, int) or val < 1:
            errors.append(f"'{key}' must be a positive whole number.")

    expiry_offset = get_setting(rules, "expiry_offset")
    if not isinstance(expiry_offset, int) or expiry_offset < 0:
        errors.append("'expiry_offset' must be a whole number >= 0.")

    max_rolls = get_setting(rules, "max_rolls_per_cycle")
    if not isinstance(max_rolls, int) or max_rolls < 0:
        errors.append("'max_rolls_per_cycle' must be a whole number >= 0.")

    for key in ("pullback_min_points", "target_capital_pct"):
        val = get_setting(rules, key)
        if not isinstance(val, (int, float)) or val <= 0:
            errors.append(f"'{key}' must be a positive number.")

    exit_days = get_setting(rules, "exit_days_before_expiry")
    if not isinstance(exit_days, int) or exit_days < 0:
        errors.append("'exit_days_before_expiry' must be a whole number >= 0.")

    return errors


def describe_otm_put_roll_rules(rules: dict | None, symbol: str = "") -> str:
    """Plain-English summary — same role as rule_schema.describe_rules() for the leg-based shape."""
    if not rules:
        return "No rules configured yet."
    lots = rules.get("lots", 1)
    otm = get_setting(rules, "initial_otm_points")
    expiry_offset = get_setting(rules, "expiry_offset")
    lookback = get_setting(rules, "pullback_lookback_days")
    min_pullback = get_setting(rules, "pullback_min_points")
    roll_points = get_setting(rules, "roll_points")
    max_rolls = get_setting(rules, "max_rolls_per_cycle")
    target = get_setting(rules, "target_capital_pct")
    exit_days = get_setting(rules, "exit_days_before_expiry")
    subject = f"{symbol} " if symbol else ""
    expiry_label = "the nearest monthly" if expiry_offset == 0 else f"the monthly expiry {expiry_offset} month(s) out"
    return (
        f"SELL {lots} lot{'s' if lots != 1 else ''} {subject}PUT {otm}pts OTM in {expiry_label}, only once spot has "
        f"pulled back at least {min_pullback}pts off its {lookback}-day high. Rolls the strike down {roll_points}pts "
        f"(up to {max_rolls}x/cycle) whenever spot closes below the current strike; exits early on a combined "
        f"+{target}% of deployed capital, or {exit_days} day(s) before expiry otherwise."
    )
