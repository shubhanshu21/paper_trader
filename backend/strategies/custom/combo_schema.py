"""
strategies/custom/combo_schema.py — the rule contract for the Nifty +
Sensex weekend-gap ratio-spread combo strategy. See
strategies/custom/weekend_combo_strategy.py for the executor and
api/weekend_combo_scheduler.py for the scheduler.

Yet another deliberately SEPARATE schema/executor/scheduler, not a
variant of rule_schema.py/RuleBasedStrategy — that whole stack assumes
ONE symbol per strategy (its `symbols` list means "replicate this same
leg structure independently on each"), but this strategy trades TWO
DIFFERENT symbols (NIFTY + SENSEX) with TWO DIFFERENT, opposite-direction
leg structures, managed as ONE combined position with a single shared
target/stop-loss across both. Forcing that into RuleBasedStrategy's
single-`self.symbol`/single-`self.strike_step`/single-`self.real_lot_size`
instance model would corrupt it instead of extending it — same reasoning
intraday_schema.py's module docstring gives for the Supertrend strategy.

The two leg structures themselves are FIXED (exact point-distances from
ATM, hardcoded per the reference strategy — see _SETUPS below), not user-
configurable the way rule_schema.py's legs are; the only real per-cycle
decision left to the user is WHICH direction to run (`bias` below) —
genuinely a subjective weekly market-view call the reference strategy
itself says to make manually, not something to infer from an indicator.
A strategy's `bias` is expected to be edited (PATCH .../rules) before
each Friday if the user's view for the coming week has changed; the
scheduler always uses whatever `bias` is configured AT the Friday entry
tick, never a stale snapshot from when the strategy was first built.

Rules JSON shape::

    {
      "bias": "NIFTY_BULLISH_SENSEX_BEARISH" | "SENSEX_BULLISH_NIFTY_BEARISH",
      "lots": {"NIFTY": <int> >= 1, "SENSEX": <int> >= 1},  # optional, default 1 each —
                                                              #   scales ALL of that symbol's
                                                              #   legs together, keeping the
                                                              #   reference strategy's 1x/2x/1x
                                                              #   ratio between them intact
      "entry_weekday": "MON".."SUN",   # default "FRI"
      "entry_time": "HH:MM",           # default "15:00"
      "exit_weekday": "MON".."SUN",    # default "TUE" — the following week's Nifty expiry day
      "exit_time": "HH:MM",            # default "15:00"
      "target_amount": <number> > 0,   # default 3000 — combined ₹ profit across BOTH symbols
      "stop_loss_amount": <number> > 0 # default 3000 — combined ₹ loss across BOTH symbols
    }

Each setup's legs (see _SETUPS): action/option_type/lots (before the
`lots` multiplier above) and `otm_points` — a POSITIVE distance from ATM
in the direction rule_schema.py's OTM_POINTS convention already uses per
option_type (CE = above spot, PE = below spot) — e.g. Setup A's Sensex
"Buy 1x Put at ATM - 200 points" is exactly {"option_type": "PE",
"otm_points": 200}, since a PE's OTM_POINTS distance is ALREADY measured
downward. This is deliberate: it lets the executor reuse rule_strategy.
resolve_leg_strike() unchanged instead of reimplementing strike math.
"""

_WEEKDAYS = {"MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"}

_DEFAULTS = {
    "entry_weekday": "FRI",
    "entry_time": "15:00",
    "exit_weekday": "TUE",
    "exit_time": "15:00",
    "target_amount": 3000,
    "stop_loss_amount": 3000,
}

_BIAS_VALUES = {"NIFTY_BULLISH_SENSEX_BEARISH", "SENSEX_BULLISH_NIFTY_BEARISH"}

# Fixed leg structures per the reference strategy — see module docstring
# for why `otm_points` is always positive and already direction-aware via
# option_type (matches rule_schema.py's OTM_POINTS convention exactly).
_SETUPS = {
    "NIFTY_BULLISH_SENSEX_BEARISH": {
        "NIFTY": [
            {"action": "BUY", "option_type": "CE", "otm_points": 100, "lots": 1},
            {"action": "SELL", "option_type": "CE", "otm_points": 250, "lots": 2},
            {"action": "BUY", "option_type": "CE", "otm_points": 350, "lots": 1},
        ],
        "SENSEX": [
            {"action": "BUY", "option_type": "PE", "otm_points": 200, "lots": 1},
            {"action": "SELL", "option_type": "PE", "otm_points": 600, "lots": 2},
            {"action": "BUY", "option_type": "PE", "otm_points": 750, "lots": 1},
        ],
    },
    "SENSEX_BULLISH_NIFTY_BEARISH": {
        "SENSEX": [
            {"action": "BUY", "option_type": "CE", "otm_points": 200, "lots": 1},
            {"action": "SELL", "option_type": "CE", "otm_points": 600, "lots": 2},
            {"action": "BUY", "option_type": "CE", "otm_points": 900, "lots": 1},
        ],
        "NIFTY": [
            {"action": "BUY", "option_type": "PE", "otm_points": 100, "lots": 1},
            {"action": "SELL", "option_type": "PE", "otm_points": 250, "lots": 2},
            {"action": "BUY", "option_type": "PE", "otm_points": 350, "lots": 1},
        ],
    },
}


def _is_hhmm(value) -> bool:
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        return False
    h, m = value[:2], value[3:]
    return h.isdigit() and m.isdigit() and 0 <= int(h) <= 23 and 0 <= int(m) <= 59


def get_setting(rules: dict, key: str):
    return rules.get(key, _DEFAULTS.get(key))


def legs_for(bias: str) -> dict[str, list[dict]]:
    """{"NIFTY": [...legs...], "SENSEX": [...legs...]} for the given bias — see _SETUPS."""
    return _SETUPS[bias]


def validate_combo_rules(rules: dict) -> list[str]:
    errors: list[str] = []
    if not isinstance(rules, dict):
        return ["Strategy rules must be an object."]

    bias = rules.get("bias")
    if bias not in _BIAS_VALUES:
        errors.append(f"'bias' must be one of {sorted(_BIAS_VALUES)}.")

    lots = rules.get("lots") or {}
    if not isinstance(lots, dict):
        errors.append("'lots' must be an object, or omitted.")
    else:
        for key in ("NIFTY", "SENSEX"):
            val = lots.get(key, 1)
            if not isinstance(val, int) or val < 1:
                errors.append(f"'lots.{key}' must be a positive whole number, or omitted.")

    for key in ("entry_weekday", "exit_weekday"):
        val = get_setting(rules, key)
        if val not in _WEEKDAYS:
            errors.append(f"'{key}' must be one of {sorted(_WEEKDAYS)}.")

    for key in ("entry_time", "exit_time"):
        val = get_setting(rules, key)
        if not _is_hhmm(val):
            errors.append(f"'{key}' must be in HH:MM format.")

    for key in ("target_amount", "stop_loss_amount"):
        val = get_setting(rules, key)
        if not isinstance(val, (int, float)) or val <= 0:
            errors.append(f"'{key}' must be a positive number.")

    return errors


def describe_combo_rules(rules: dict | None, symbol: str = "") -> str:
    if not rules:
        return "No rules configured yet."
    bias = rules.get("bias")
    entry_weekday, entry_time = get_setting(rules, "entry_weekday"), get_setting(rules, "entry_time")
    exit_weekday, exit_time = get_setting(rules, "exit_weekday"), get_setting(rules, "exit_time")
    target, stop_loss = get_setting(rules, "target_amount"), get_setting(rules, "stop_loss_amount")
    bull, bear = ("NIFTY", "SENSEX") if bias == "NIFTY_BULLISH_SENSEX_BEARISH" else ("SENSEX", "NIFTY")
    return (
        f"{bull} bullish call ratio spread + {bear} bearish put ratio spread (1x/2x/1x each), entered together "
        f"{entry_weekday.title()} {entry_time}, exited {exit_weekday.title()} {exit_time} or on a combined "
        f"+₹{target:,.0f} profit / -₹{stop_loss:,.0f} loss across both — whichever comes first."
    )
