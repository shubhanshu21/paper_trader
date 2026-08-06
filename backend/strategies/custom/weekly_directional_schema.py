"""
strategies/custom/weekly_directional_schema.py — the rule contract for
the "Weekly Directional" strategy (a modified, asymmetric Reverse Iron
Fly with delta-targeted tail hedges, EMA-crossover directional bias). See
weekly_directional_strategy.py for the executor and
api/weekly_directional_engine.py for the tick function (registered in
api/strategy_scheduler.py's dispatch — see engine_registry.py for the
matching route-layer registration).

A NINTH separate engine, not a rule_schema.py leg combination — the leg-
based builder assumes a FIXED leg list decided at strategy-creation time;
this strategy's leg list ITSELF changes shape every cycle depending on a
live directional signal (which side gets 2 lots sold vs 1, which side
gets the tail hedge) — there's no way to express "build these 5 legs, but
swap which two are 2x and which OTM side gets hedged, based on today's
EMA read" in that static shape. The delta-targeted tail-hedge strike
search (matching delta_neutral_strategy.py's own Black-76 discipline) is
also foreign to the leg builder's strike_selection modes (ATM/OTM_PERCENT/
OTM_POINTS/FIXED/PREMIUM_OFFSET/PREMIUM_BAND — none solve for a target
delta).

Direction: EMA(ema_fast) vs EMA(ema_slow) on the underlying's own daily
closes, read fresh at each weekly entry check (never persisted as a
"currently crossed" flag, so a missed tick/restart can't get stuck) —
BULLISH if fast > slow, BEARISH if fast < slow, no entry if exactly
equal. This is a CURRENT-STATE trend bias, not "did the exact crossover
happen on this specific tick" — since entry is only evaluated once a
week (entry_weekday/entry_time), requiring the literal crossing to fall
inside that one weekly check would miss it almost every time. The video
this strategy is modeled on explicitly frames the EMA read as swappable
for "any technical indicator or price action trend bias" — current-state
EMA order is the simplest faithful implementation of that framing.

Rules JSON shape::

    {
      "lots": <int> >= 1,               # base (1x) lot count — the ATM straddle
                                          #   legs and the LIGHTER-sold OTM leg
                                          #   trade this many; the HEAVIER-sold
                                          #   OTM leg and the tail hedge trade
                                          #   2x this
      "ema_fast": <int> >= 1,            # default 20
      "ema_slow": <int> >= 1,            # default 50, must be > ema_fast
      "expiry_offset": <int> >= 0,       # default 0 — which weekly expiry to
                                          #   trade (0 = nearest)
      "short_otm_points": <number> > 0,  # default 250 — how far OTM (in index
                                          #   points) both asymmetric short
                                          #   strikes sit from ATM
      "tail_hedge_target_delta": <number> in (0, 1), # default 0.05 — the tail
                                          #   hedge strike is the one whose live
                                          #   |delta| is closest to this
      "entry_weekday": "MON".."SUN",     # default "MON"
      "entry_time": "HH:MM",             # default "09:20"
      "target_capital_pct": <number> > 0,# default 10 — close everything once
                                          #   this cycle's cumulative P&L reaches
                                          #   this % of the real broker margin
                                          #   captured at entry (the video's own
                                          #   "10-12% return on margin" target)
      "stop_loss_capital_pct": <number> > 0, # default 5 — same, on the downside
      "exit_days_before_expiry": <int> >= 0, # default 0 — hard stop; 0 means
                                          #   hold through the traded expiry
                                          #   itself (a genuinely weekly-hold
                                          #   strategy, per the video)
    }
"""

_DEFAULTS = {
    "ema_fast": 20,
    "ema_slow": 50,
    "expiry_offset": 0,
    "short_otm_points": 250,
    "tail_hedge_target_delta": 0.05,
    "entry_weekday": "MON",
    "entry_time": "09:20",
    "target_capital_pct": 10,
    "stop_loss_capital_pct": 5,
    "exit_days_before_expiry": 0,
}

_WEEKDAYS = {"MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"}


def get_setting(rules: dict, key: str):
    return rules.get(key, _DEFAULTS.get(key))


def validate_weekly_directional_rules(rules: dict) -> list[str]:
    """Return a list of human-readable error strings; empty list = valid. Mirrors rule_schema.validate_rules()'s contract."""
    errors: list[str] = []
    if not isinstance(rules, dict):
        return ["Strategy rules must be an object."]

    lots = rules.get("lots", 1)
    if not isinstance(lots, int) or lots < 1:
        errors.append("'lots' must be a positive whole number.")

    ema_fast = get_setting(rules, "ema_fast")
    ema_slow = get_setting(rules, "ema_slow")
    if not isinstance(ema_fast, int) or ema_fast < 1:
        errors.append("'ema_fast' must be a positive whole number.")
    if not isinstance(ema_slow, int) or ema_slow < 1:
        errors.append("'ema_slow' must be a positive whole number.")
    if isinstance(ema_fast, int) and isinstance(ema_slow, int) and ema_fast >= ema_slow:
        errors.append("'ema_slow' must be greater than 'ema_fast'.")

    expiry_offset = get_setting(rules, "expiry_offset")
    if not isinstance(expiry_offset, int) or expiry_offset < 0:
        errors.append("'expiry_offset' must be a whole number >= 0.")

    otm_points = get_setting(rules, "short_otm_points")
    if not isinstance(otm_points, (int, float)) or otm_points <= 0:
        errors.append("'short_otm_points' must be a positive number.")

    target_delta = get_setting(rules, "tail_hedge_target_delta")
    if not isinstance(target_delta, (int, float)) or not (0 < target_delta < 1):
        errors.append("'tail_hedge_target_delta' must be a number strictly between 0 and 1.")

    if get_setting(rules, "entry_weekday") not in _WEEKDAYS:
        errors.append(f"'entry_weekday' must be one of {sorted(_WEEKDAYS)}.")

    entry_time = get_setting(rules, "entry_time")
    if not isinstance(entry_time, str) or len(entry_time) != 5 or entry_time[2] != ":":
        errors.append("'entry_time' must be an 'HH:MM' time string.")

    for key in ("target_capital_pct", "stop_loss_capital_pct"):
        val = get_setting(rules, key)
        if not isinstance(val, (int, float)) or val <= 0:
            errors.append(f"'{key}' must be a positive number.")

    exit_days = get_setting(rules, "exit_days_before_expiry")
    if not isinstance(exit_days, int) or exit_days < 0:
        errors.append("'exit_days_before_expiry' must be a whole number >= 0.")

    return errors


def describe_weekly_directional_rules(rules: dict | None, symbol: str = "") -> str:
    """Plain-English summary — same role as rule_schema.describe_rules() for the leg-based shape."""
    if not rules:
        return "No rules configured yet."
    lots = rules.get("lots", 1)
    ema_fast, ema_slow = get_setting(rules, "ema_fast"), get_setting(rules, "ema_slow")
    otm_points = get_setting(rules, "short_otm_points")
    target_delta = get_setting(rules, "tail_hedge_target_delta")
    entry_wd, entry_time = get_setting(rules, "entry_weekday"), get_setting(rules, "entry_time")
    target, stop = get_setting(rules, "target_capital_pct"), get_setting(rules, "stop_loss_capital_pct")
    exit_days = get_setting(rules, "exit_days_before_expiry")
    subject = f"{symbol} " if symbol else ""
    return (
        f"BUY {lots} lot{'s' if lots != 1 else ''} {subject}ATM straddle, funded by SELLING an asymmetric pair of "
        f"OTM strikes {otm_points}pts either side of ATM (2x lots on whichever side EMA({ema_fast})/EMA({ema_slow}) "
        f"favors betting against, 1x on the other), plus a 2x deep-OTM (~{target_delta} delta) tail hedge on the "
        f"heavier-sold side, entered {entry_wd} at {entry_time} once a clear EMA bias exists. Exits at "
        f"+{target}%/-{stop}% of deployed capital, or "
        f"{'on the traded expiry itself' if exit_days == 0 else f'{exit_days} day(s) before expiry'} regardless."
    )
