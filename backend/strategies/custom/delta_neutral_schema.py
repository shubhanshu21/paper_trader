"""
strategies/custom/delta_neutral_schema.py — the rule contract for the
Monthly Delta-Neutral Strangle with Dynamic Adjustment strategy. See
delta_neutral_strategy.py for the executor and
api/delta_neutral_engine.py for the tick function (registered in
api/strategy_scheduler.py's dispatch — see engine_registry.py for the
matching route-layer registration).

A NINTH separate engine — none of the following has any equivalent in
the leg-based engine's static entry/exit model:
  - strikes chosen by LIVE DELTA (Black-76, solved from the real traded
    premium), not a fixed points/percent/premium offset — and re-checked
    against a moving delta threshold every tick, not just at entry.
  - a TWO-STAGE escalating adjustment: Stage 1 (a premium-ratio trigger)
    partially rebalances one side by matching the OTHER side's live
    delta; Stage 2 (a delta-threshold trigger, independent of whether
    Stage 1 ever fired) closes EVERYTHING and re-opens both legs chosen
    by PREMIUM this time, not delta — two different strike-selection
    rules chained together based on which trigger condition is met.
  - a "3rd weekly expiry of the month" time exit — a calendar rule
    that's neither a fixed weekday nor an expiry-relative buffer, unlike
    anything the other engines' exit rules express.

Rules JSON shape::

    {
      "lots": <int> >= 1,
      "target_delta": <number> 0 < x < 1,    # default 0.15 — |delta| each short leg
                                              #   targets at entry
      "strike_grid": <int> >= 1,             # default 100 — only strikes that are a
                                              #   multiple of this are considered (the
                                              #   reference strategy's own "100-strike
                                              #   intervals for liquidity" rule)
      "hedge_premium_min": <number> > 0,     # default 1 — live-premium band (₹) for the
      "hedge_premium_max": <number> > 0,     #   cheap OTM margin-benefit hedge legs —
                                              #   default 5 (see session_seller_schema.py's
                                              #   identical search discipline)
      "entry_time": "HH:MM",                 # default "09:20" — entry window OPENS here
      "entry_time_end": "HH:MM",             # default "10:00" — and CLOSES here (the
                                              #   reference strategy's own 09:20-10:00
                                              #   window, the day after the previous
                                              #   monthly expiry)
      "premium_ratio_trigger": <number> > 1, # default 2.0 — Stage 1: fires once
                                              #   max(short_ce_premium, short_pe_premium) /
                                              #   min(...) reaches this
      "delta_trigger_min": <number> 0 < x < 1, # default 0.45 — Stage 2: fires once the
      "delta_trigger_max": <number> 0 < x < 1, #   LOSING short leg's |delta| enters
                                              #   [delta_trigger_min, delta_trigger_max]
                                              #   (default 0.45-0.50 — "becomes ATM")
      "reset_premium_pct": <number> > 0,     # default 50 — Stage 2's re-sold legs each
                                              #   target a premium equal to this % of the
                                              #   losing leg's OWN exit price
      "target_capital_pct": <number> > 0,    # default 5 — close everything once this
                                              #   cycle's cumulative P&L reaches this % of
                                              #   the REAL broker margin captured at entry
      "third_weekly_exit_time": "HH:MM"      # default "15:15" — force-close everything on
                                              #   the month's 3RD weekly expiry (never the
                                              #   4th/last), regardless of target/adjustment
                                              #   state — "stay away from 4th-week rollover
                                              #   volatility"; re-entry only next monthly cycle
    }
"""

_DEFAULTS = {
    "target_delta": 0.15,
    "strike_grid": 100,
    "hedge_premium_min": 1,
    "hedge_premium_max": 5,
    "entry_time": "09:20",
    "entry_time_end": "10:00",
    "premium_ratio_trigger": 2.0,
    "delta_trigger_min": 0.45,
    "delta_trigger_max": 0.50,
    "reset_premium_pct": 50,
    "target_capital_pct": 5,
    "third_weekly_exit_time": "15:15",
}


def get_setting(rules: dict, key: str):
    return rules.get(key, _DEFAULTS.get(key))


def _is_hhmm(value) -> bool:
    return isinstance(value, str) and len(value) == 5 and value[2] == ":"


def validate_delta_neutral_rules(rules: dict) -> list[str]:
    """Return a list of human-readable error strings; empty list = valid. Mirrors rule_schema.validate_rules()'s contract."""
    errors: list[str] = []
    if not isinstance(rules, dict):
        return ["Strategy rules must be an object."]

    lots = rules.get("lots", 1)
    if not isinstance(lots, int) or lots < 1:
        errors.append("'lots' must be a positive whole number.")

    for key in ("target_delta", "delta_trigger_min", "delta_trigger_max"):
        val = get_setting(rules, key)
        if not isinstance(val, (int, float)) or not (0 < val < 1):
            errors.append(f"'{key}' must be a number strictly between 0 and 1.")

    delta_min, delta_max = get_setting(rules, "delta_trigger_min"), get_setting(rules, "delta_trigger_max")
    if isinstance(delta_min, (int, float)) and isinstance(delta_max, (int, float)) and delta_min > delta_max:
        errors.append("'delta_trigger_min' must not be greater than 'delta_trigger_max'.")

    if not isinstance(get_setting(rules, "strike_grid"), int) or get_setting(rules, "strike_grid") < 1:
        errors.append("'strike_grid' must be a positive whole number.")

    for key in ("hedge_premium_min", "hedge_premium_max", "reset_premium_pct", "target_capital_pct"):
        val = get_setting(rules, key)
        if not isinstance(val, (int, float)) or val <= 0:
            errors.append(f"'{key}' must be a positive number.")

    hedge_min, hedge_max = get_setting(rules, "hedge_premium_min"), get_setting(rules, "hedge_premium_max")
    if isinstance(hedge_min, (int, float)) and isinstance(hedge_max, (int, float)) and hedge_min > hedge_max:
        errors.append("'hedge_premium_min' must not be greater than 'hedge_premium_max'.")

    for key in ("entry_time", "entry_time_end", "third_weekly_exit_time"):
        if not _is_hhmm(get_setting(rules, key)):
            errors.append(f"'{key}' must be an 'HH:MM' time string.")

    ratio = get_setting(rules, "premium_ratio_trigger")
    if not isinstance(ratio, (int, float)) or ratio <= 1:
        errors.append("'premium_ratio_trigger' must be a number greater than 1.")

    return errors


def describe_delta_neutral_rules(rules: dict | None, symbol: str = "") -> str:
    """Plain-English summary — same role as rule_schema.describe_rules() for the leg-based shape."""
    if not rules:
        return "No rules configured yet."
    lots = rules.get("lots", 1)
    target_delta = get_setting(rules, "target_delta")
    grid = get_setting(rules, "strike_grid")
    entry_start, entry_end = get_setting(rules, "entry_time"), get_setting(rules, "entry_time_end")
    ratio = get_setting(rules, "premium_ratio_trigger")
    dmin, dmax = get_setting(rules, "delta_trigger_min"), get_setting(rules, "delta_trigger_max")
    reset_pct = get_setting(rules, "reset_premium_pct")
    target = get_setting(rules, "target_capital_pct")
    exit_time = get_setting(rules, "third_weekly_exit_time")
    subject = f"{symbol} " if symbol else ""
    return (
        f"SELL {lots} lot{'s' if lots != 1 else ''} {subject}{target_delta} delta CALL + {target_delta} delta PUT "
        f"(strikes on a {grid}pt grid), entered {entry_start}-{entry_end} the day after the previous monthly "
        f"expiry, plus cheap OTM hedges for margin. Stage 1: rebalances (books the winning leg, re-sells matching "
        f"the losing leg's live delta) once the short-leg premium ratio hits {ratio}x. Stage 2 (independent of "
        f"Stage 1): a full reset — closes everything, re-sells both legs at {reset_pct}% of the losing leg's exit "
        f"premium — once the losing leg's delta reaches {dmin}-{dmax}. Exits on +{target}% of deployed capital, "
        f"or {exit_time} on the month's 3rd weekly expiry regardless."
    )
