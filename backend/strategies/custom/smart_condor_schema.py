"""
strategies/custom/smart_condor_schema.py — the rule contract for the
"Smart Condor" weekly iron-condor strategy with a mechanical, capped
premium-ratio adjustment protocol. See smart_condor_strategy.py for the
executor and api/smart_condor_engine.py for the tick function (registered
in api/strategy_scheduler.py's dispatch — see engine_registry.py for the
matching route-layer registration).

A FIFTH separate engine, not a variant of rule_schema.py — an Iron Condor
IS one of rule_schema.py's own templates (see routes_custom_strategies.py's
IRON_CONDOR template) when the strikes are fixed at entry and never
touched again. What breaks the general engine's "enter once, exit once"
model here is the mechanical MID-CYCLE adjustment: whenever the ratio
between the two short legs' live premiums breaches premium_ratio_trigger,
the cheaper (safe) side's short+hedge pair is closed and REPLACED with a
new short (strike chosen to match the threatened side's current premium)
plus a new hedge — up to max_adjustments_per_cycle times, after which the
NEXT breach forces a full close instead of another adjustment. None of
that — evaluating a live cross-leg premium ratio, swapping two legs for
two different ones, or counting adjustments toward a forced-exit cap — has
any equivalent in rule_schema.py's static entry/exit model.

Rules JSON shape::

    {
      "lots": <int> >= 1,
      "dte_weeks_offset": <int> >= 0,    # default 1 — which weekly expiry to trade:
                                          #   0 = the nearest one listed, 1 = the one
                                          #   after that ("next week's", ~8 DTE at a
                                          #   Monday entry). See find_weekly_expiries.
      "premium_round_points": <int> >= 1,# default 100 — the live ATM straddle premium
                                          #   (ATM CE LTP + ATM PE LTP) is rounded to the
                                          #   nearest multiple of this before being used
                                          #   as the short strikes' offset from ATM
                                          #   (e.g. a live 487 premium rounds to 500)
      "hedge_points": <int> >= 1,        # default 200 — how far beyond each short
                                          #   strike its protective long hedge sits
      "entry_weekday": "MON".."SUN",     # default "MON" — the one weekday entry is
                                          #   attempted on each cycle
      "entry_time": "HH:MM",             # default "10:15"
      "exit_weekday": "MON".."SUN",      # default "FRI" — forced exit deadline,
                                          #   independent of the traded contract's own
                                          #   expiry date (a next-week contract is still
                                          #   open on this Friday) — this is a calendar
                                          #   holding-period cap, not an expiry buffer
      "exit_time": "HH:MM",              # default "14:30" — combined with exit_weekday
      "target_capital_pct": <number> > 0,# default 1 — close everything once this
                                          #   cycle's cumulative P&L reaches this % of
                                          #   the real broker margin captured at entry
      "stop_loss_capital_pct": <number> > 0, # default 1 — same, on the downside
      "premium_ratio_trigger": <number> > 1, # default 1.8 — adjust once
                                          #   max(short_ce_ltp, short_pe_ltp) /
                                          #   min(...) reaches this
      "max_adjustments_per_cycle": <int> >= 0, # default 2 — the (N+1)th trigger this
                                          #   cycle forces a full close instead of
                                          #   another adjustment
    }
"""

_DEFAULTS = {
    "dte_weeks_offset": 1,
    "premium_round_points": 100,
    "hedge_points": 200,
    "entry_weekday": "MON",
    "entry_time": "10:15",
    "exit_weekday": "FRI",
    "exit_time": "14:30",
    "target_capital_pct": 1,
    "stop_loss_capital_pct": 1,
    "premium_ratio_trigger": 1.8,
    "max_adjustments_per_cycle": 2,
}

_WEEKDAYS = {"MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"}


def get_setting(rules: dict, key: str):
    return rules.get(key, _DEFAULTS.get(key))


def validate_smart_condor_rules(rules: dict) -> list[str]:
    """Return a list of human-readable error strings; empty list = valid. Mirrors rule_schema.validate_rules()'s contract."""
    errors: list[str] = []
    if not isinstance(rules, dict):
        return ["Strategy rules must be an object."]

    lots = rules.get("lots", 1)
    if not isinstance(lots, int) or lots < 1:
        errors.append("'lots' must be a positive whole number.")

    dte_offset = get_setting(rules, "dte_weeks_offset")
    if not isinstance(dte_offset, int) or dte_offset < 0:
        errors.append("'dte_weeks_offset' must be a whole number >= 0.")

    for key in ("premium_round_points", "hedge_points"):
        val = get_setting(rules, key)
        if not isinstance(val, int) or val < 1:
            errors.append(f"'{key}' must be a positive whole number.")

    max_adj = get_setting(rules, "max_adjustments_per_cycle")
    if not isinstance(max_adj, int) or max_adj < 0:
        errors.append("'max_adjustments_per_cycle' must be a whole number >= 0.")

    for key in ("entry_weekday", "exit_weekday"):
        val = get_setting(rules, key)
        if val not in _WEEKDAYS:
            errors.append(f"'{key}' must be one of {sorted(_WEEKDAYS)}.")

    for key in ("entry_time", "exit_time"):
        val = get_setting(rules, key)
        if not isinstance(val, str) or len(val) != 5 or val[2] != ":":
            errors.append(f"'{key}' must be an 'HH:MM' time string.")

    for key in ("target_capital_pct", "stop_loss_capital_pct"):
        val = get_setting(rules, key)
        if not isinstance(val, (int, float)) or val <= 0:
            errors.append(f"'{key}' must be a positive number.")

    ratio = get_setting(rules, "premium_ratio_trigger")
    if not isinstance(ratio, (int, float)) or ratio <= 1:
        errors.append("'premium_ratio_trigger' must be a number greater than 1.")

    return errors


def describe_smart_condor_rules(rules: dict | None, symbol: str = "") -> str:
    """Plain-English summary — same role as rule_schema.describe_rules() for the leg-based shape."""
    if not rules:
        return "No rules configured yet."
    lots = rules.get("lots", 1)
    dte_offset = get_setting(rules, "dte_weeks_offset")
    hedge = get_setting(rules, "hedge_points")
    entry_wd = get_setting(rules, "entry_weekday")
    entry_time = get_setting(rules, "entry_time")
    exit_wd = get_setting(rules, "exit_weekday")
    exit_time = get_setting(rules, "exit_time")
    target = get_setting(rules, "target_capital_pct")
    stop = get_setting(rules, "stop_loss_capital_pct")
    ratio = get_setting(rules, "premium_ratio_trigger")
    max_adj = get_setting(rules, "max_adjustments_per_cycle")
    subject = f"{symbol} " if symbol else ""
    expiry_label = "the nearest weekly" if dte_offset == 0 else f"the weekly expiry {dte_offset} week(s) out"
    return (
        f"SELL {lots} lot{'s' if lots != 1 else ''} {subject}ATM-straddle-width iron condor (short strikes = ATM "
        f"+/- the live ATM straddle premium, hedges {hedge}pts further out) in {expiry_label}, entering "
        f"{entry_wd} at {entry_time}. Adjusts (closes the cheaper side, re-sells it to match the threatened "
        f"side's premium, re-hedges) whenever the short-leg premium ratio hits {ratio}x, up to {max_adj} times "
        f"per cycle — the next trigger after that forces a full close instead. Otherwise exits on "
        f"+{target}%/-{stop}% of deployed capital, or by {exit_wd} {exit_time} regardless."
    )
