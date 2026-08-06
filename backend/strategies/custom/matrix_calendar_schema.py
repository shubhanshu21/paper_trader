"""
strategies/custom/matrix_calendar_schema.py — the rule contract for the
"Matrix Calendar" zero-adjustment weekly options-selling strategy (a
hybrid Ratio Calendar / Iron Condor: delta-targeted weekly short strangle
+ weekly outer hedges + MONTHLY same-strike calendar hedges, sized for a
positive Vega profile). See matrix_calendar_strategy.py for the executor
and api/matrix_calendar_engine.py for the tick function (registered in
api/strategy_scheduler.py's dispatch — see engine_registry.py for the
matching route-layer registration).

A TENTH separate engine, not a rule_schema.py leg combination — two
things break the leg builder's static single-expiry model here: (1) the
short strikes are DELTA-TARGETED (~0.23 delta), a strike_selection mode
the leg builder has never had (same gap delta_neutral_strategy.py needed
its own engine for); (2) the monthly hedge legs must resolve to the EXACT
SAME strike price the weekly short legs dynamically land on, just in a
different (monthly) expiry — the leg builder's per-leg expiry_mode
override already supports MULTIPLE expiries in one strategy (see
custom_strategy_scheduler.py's _leg_groups(), built for calendar spreads),
but it has no way for one leg's strike to be DERIVED from another leg's
dynamically-resolved strike; every leg resolves its own strike
independently.

Rules JSON shape::

    {
      "lots": <int> >= 1,                # base lot count — the short CE/PE
                                          #   legs trade 2x this, every hedge
                                          #   leg (weekly outer + monthly
                                          #   calendar) trades 1x
      "short_target_delta": <number> in (0,1), # default 0.23 — the weekly
                                          #   short CE/PE strikes are
                                          #   whichever strike's live |delta|
                                          #   is closest to this
      "strike_grid": <int> >= 1,         # default 100 — only strikes on this
                                          #   grid are considered for the
                                          #   delta search (liquidity —
                                          #   see the video's own "100pt not
                                          #   50pt" note)
      "weekly_hedge_points": <number> > 0,# default 500 — how far OTM (past
                                          #   the sold strike) each weekly
                                          #   outer hedge sits
      "weekly_expiry_offset": <int> >= 0,# default 1 — which weekly expiry
                                          #   to trade (0 = nearest, 1 = the
                                          #   one after — "~8 DTE" off a
                                          #   Monday entry per the video)
      "entry_weekday": "MON".."SUN",     # default "MON"
      "entry_time": "HH:MM",             # default "15:16"
      "max_hold_days": <int> >= 1,       # default 2 — forced exit this many
                                          #   CALENDAR days after entry,
                                          #   regardless of P&L (the video's
                                          #   own "1-2 days max" — this is a
                                          #   holding-period cap, not an
                                          #   expiry buffer)
      "target_capital_pct": <number> > 0,# default 1.5 — close everything
                                          #   once this cycle's cumulative
                                          #   P&L reaches this % of the real
                                          #   broker margin captured at entry
      "stop_loss_capital_pct": <number> > 0, # default 2 — same, downside
      "exit_days_before_expiry": <int> >= 0, # default 1 — safety-net hard
                                          #   stop on the WEEKLY leg's own
                                          #   expiry, in case max_hold_days
                                          #   somehow doesn't fire (e.g.
                                          #   process downtime) — should
                                          #   never normally trigger given
                                          #   the 1-2 day hold
    }
"""

_DEFAULTS = {
    "short_target_delta": 0.23,
    "strike_grid": 100,
    "weekly_hedge_points": 500,
    "weekly_expiry_offset": 1,
    "entry_weekday": "MON",
    "entry_time": "15:16",
    "max_hold_days": 2,
    "target_capital_pct": 1.5,
    "stop_loss_capital_pct": 2,
    "exit_days_before_expiry": 1,
}

_WEEKDAYS = {"MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"}


def get_setting(rules: dict, key: str):
    return rules.get(key, _DEFAULTS.get(key))


def validate_matrix_calendar_rules(rules: dict) -> list[str]:
    """Return a list of human-readable error strings; empty list = valid. Mirrors rule_schema.validate_rules()'s contract."""
    errors: list[str] = []
    if not isinstance(rules, dict):
        return ["Strategy rules must be an object."]

    lots = rules.get("lots", 1)
    if not isinstance(lots, int) or lots < 1:
        errors.append("'lots' must be a positive whole number.")

    target_delta = get_setting(rules, "short_target_delta")
    if not isinstance(target_delta, (int, float)) or not (0 < target_delta < 1):
        errors.append("'short_target_delta' must be a number strictly between 0 and 1.")

    strike_grid = get_setting(rules, "strike_grid")
    if not isinstance(strike_grid, int) or strike_grid < 1:
        errors.append("'strike_grid' must be a positive whole number.")

    hedge_points = get_setting(rules, "weekly_hedge_points")
    if not isinstance(hedge_points, (int, float)) or hedge_points <= 0:
        errors.append("'weekly_hedge_points' must be a positive number.")

    expiry_offset = get_setting(rules, "weekly_expiry_offset")
    if not isinstance(expiry_offset, int) or expiry_offset < 0:
        errors.append("'weekly_expiry_offset' must be a whole number >= 0.")

    if get_setting(rules, "entry_weekday") not in _WEEKDAYS:
        errors.append(f"'entry_weekday' must be one of {sorted(_WEEKDAYS)}.")

    entry_time = get_setting(rules, "entry_time")
    if not isinstance(entry_time, str) or len(entry_time) != 5 or entry_time[2] != ":":
        errors.append("'entry_time' must be an 'HH:MM' time string.")

    max_hold = get_setting(rules, "max_hold_days")
    if not isinstance(max_hold, int) or max_hold < 1:
        errors.append("'max_hold_days' must be a whole number >= 1.")

    for key in ("target_capital_pct", "stop_loss_capital_pct"):
        val = get_setting(rules, key)
        if not isinstance(val, (int, float)) or val <= 0:
            errors.append(f"'{key}' must be a positive number.")

    exit_days = get_setting(rules, "exit_days_before_expiry")
    if not isinstance(exit_days, int) or exit_days < 0:
        errors.append("'exit_days_before_expiry' must be a whole number >= 0.")

    return errors


def describe_matrix_calendar_rules(rules: dict | None, symbol: str = "") -> str:
    """Plain-English summary — same role as rule_schema.describe_rules() for the leg-based shape."""
    if not rules:
        return "No rules configured yet."
    lots = rules.get("lots", 1)
    target_delta = get_setting(rules, "short_target_delta")
    hedge_points = get_setting(rules, "weekly_hedge_points")
    entry_wd, entry_time = get_setting(rules, "entry_weekday"), get_setting(rules, "entry_time")
    max_hold = get_setting(rules, "max_hold_days")
    target, stop = get_setting(rules, "target_capital_pct"), get_setting(rules, "stop_loss_capital_pct")
    subject = f"{symbol} " if symbol else ""
    return (
        f"SELL {2 * lots} lot{'s' if 2 * lots != 1 else ''} {subject}weekly ATM-ish CE + PE (whichever strikes sit "
        f"closest to {target_delta} delta), hedge each with {lots} lot{'s' if lots != 1 else ''} bought {hedge_points}pts "
        f"further OTM in the SAME weekly expiry, plus {lots} lot{'s' if lots != 1 else ''} bought at the EXACT SAME "
        f"strikes in the MONTHLY expiry (the calendar legs that keep this Vega-positive against IV spikes/gaps). "
        f"Entered {entry_wd} at {entry_time}, zero adjustments — exits at +{target}%/-{stop}% of deployed capital "
        f"or after {max_hold} day{'s' if max_hold != 1 else ''}, whichever comes first."
    )
