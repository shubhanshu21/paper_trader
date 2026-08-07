"""
strategies/custom/zero_to_hero_schema.py — the rule contract for the
"Zero to Hero" 3-step PDH/PDL breakout-pullback option BUYING strategy
(AbhishekXTrades' "3 Step Entry-Exit for Nifty Options"). See
strategies/custom/zero_to_hero_strategy.py for the executor and
api/zero_to_hero_engine.py for the scheduler that drives it.

Deliberately its OWN schema/executor/scheduler, not a variant of
rule_schema.py/RuleBasedStrategy/custom_strategy_scheduler.py — same
reasoning as intraday_schema.py's own docstring: which option_type to
BUY is decided LIVE by a candle-pattern signal (not fixed at build
time), entries are gated on a previous-day-high/low bias plus a
pullback-candle count, and the exit shape is unique to this strategy —
a hard price-level stop, a 50% partial book at 1:1 risk:reward, the
remainder held to a fixed time exit, and at most one same-day re-entry
gated on whether 1:1 was already reached before the stop hit. None of
rule_schema's %/₹/breakeven/time exit shapes represent that, and this
is also the only strategy in this app that BUYS options to open rather
than sells (see zero_to_hero_strategy.py's own docstring on why that's
safe) — force-fitting it into the leg-based model would corrupt both
its entry-signal and its exit-shape invariants.

A CustomStrategy row using this schema is marked by
`strategy_type == "ZERO_TO_HERO"` (see db/models.py) — that value is
what routes custom_strategy_scheduler.py to SKIP it and
api/zero_to_hero_engine.py to pick it up instead (dispatched via
api/strategy_scheduler.py's shared tick loop, same as every other
signal-driven engine). instrument_type/symbols (exactly one symbol
expected) still live on the CustomStrategy row itself, same columns
every other custom strategy uses.

Rules JSON shape::

    {
      "lots": <even int> >= 2,              # split 50/50 into a "book at 1:1" half and a
                                             # "hold to exit_time" half — MUST be even (see
                                             # validate_zero_to_hero_rules) since this strategy's
                                             # whole partial-booking mechanic requires an exact
                                             # half; an odd lot count has no valid 50% split.
      "candle_interval_minutes": <int> >= 1, # default 15 — the reference strategy's own timeframe
      "sl_buffer_points": <number> > 0,      # default 5 — added beyond the entry candle's high
                                             # (PE)/low (CE) so normal wick noise doesn't stop
                                             # the trade out at the exact structural level
      "max_pullback_candles": <int> >= 1,    # default 2 — 1-2 counter-color candles is a valid
                                             # pullback; 3+ cancels the setup (treated as a
                                             # reversal, not a pullback)
      "expiry_offset": <int> >= 0,           # default 0 — 0 = nearest weekly
      "exit_time": "HH:MM",                  # default "15:15" — square off whatever's left
                                             # (the "runner" half) by this time, no overnight
      "max_reentries": <int> >= 0,           # default 1 — one re-entry allowed per day, and
                                             # ONLY if the stop was hit before 1:1 RR was ever
                                             # reached that day (see zero_to_hero_engine.py)
    }
"""

_DEFAULTS = {
    "candle_interval_minutes": 15,
    "sl_buffer_points": 5,
    "max_pullback_candles": 2,
    "expiry_offset": 0,
    "exit_time": "15:15",
    "max_reentries": 1,
}


def _is_hhmm(value) -> bool:
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        return False
    h, m = value[:2], value[3:]
    return h.isdigit() and m.isdigit() and 0 <= int(h) <= 23 and 0 <= int(m) <= 59


def get_setting(rules: dict, key: str):
    """Every field except `lots` has a documented default — this is the one place that default is applied, so the executor/scheduler/description never have to repeat it."""
    return rules.get(key, _DEFAULTS.get(key))


def validate_zero_to_hero_rules(rules: dict) -> list[str]:
    """Return a list of human-readable error strings; empty list = valid. Mirrors intraday_schema.validate_intraday_rules()'s contract exactly, just for this shape."""
    errors: list[str] = []
    if not isinstance(rules, dict):
        return ["Strategy rules must be an object."]

    lots = rules.get("lots", 2)
    if not isinstance(lots, int) or lots < 2 or lots % 2 != 0:
        errors.append("'lots' must be an even whole number of 2 or more (it splits 50/50 into a booked half and a runner half).")

    interval = get_setting(rules, "candle_interval_minutes")
    if not isinstance(interval, int) or interval < 1:
        errors.append("'candle_interval_minutes' must be a positive whole number.")

    buffer = get_setting(rules, "sl_buffer_points")
    if not isinstance(buffer, (int, float)) or buffer <= 0:
        errors.append("'sl_buffer_points' must be a positive number.")

    max_pullback = get_setting(rules, "max_pullback_candles")
    if not isinstance(max_pullback, int) or max_pullback < 1:
        errors.append("'max_pullback_candles' must be a positive whole number.")

    expiry_offset = get_setting(rules, "expiry_offset")
    if not isinstance(expiry_offset, int) or expiry_offset < 0:
        errors.append("'expiry_offset' must be a non-negative whole number.")

    exit_time = get_setting(rules, "exit_time")
    if not _is_hhmm(exit_time):
        errors.append("'exit_time' must be in HH:MM format.")

    max_reentries = get_setting(rules, "max_reentries")
    if not isinstance(max_reentries, int) or max_reentries < 0:
        errors.append("'max_reentries' must be a non-negative whole number.")

    return errors


def describe_zero_to_hero_rules(rules: dict | None, symbol: str = "") -> str:
    """Plain-English summary — same role as intraday_schema.describe_intraday_rules() for this shape."""
    if not rules:
        return "No rules configured yet."
    lots = rules.get("lots", 2)
    interval = get_setting(rules, "candle_interval_minutes")
    max_pullback = get_setting(rules, "max_pullback_candles")
    exit_time = get_setting(rules, "exit_time")
    max_reentries = get_setting(rules, "max_reentries")
    subject = f"{symbol} " if symbol else ""
    return (
        f"BUY {lots} lots of {subject}ATM CALL when price is above the previous day's high and a "
        f"{interval}-min green candle closes above the open of a 1-{max_pullback} candle red pullback "
        f"(mirror image with PUT below the previous day's low). Stop beyond the entry candle's low/high "
        f"plus a buffer; book half at 1:1 risk:reward, hold the rest to {exit_time}; up to {max_reentries} "
        f"re-entr{'y' if max_reentries == 1 else 'ies'} today if stopped out before 1:1 is reached."
    )
