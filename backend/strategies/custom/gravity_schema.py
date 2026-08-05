"""
strategies/custom/gravity_schema.py — the rule contract for the "Gravity"
Camarilla fakeout-reversal credit-spread strategy. See gravity_strategy.py
for the executor and api/gravity_engine.py for the tick function
(registered in api/strategy_scheduler.py's dispatch — see
engine_registry.py for the matching route-layer registration).

A SIXTH separate engine, not a variant of rule_schema.py — none of the
following has any equivalent in the static leg-based engine's entry model:
  - a STATEFUL daily-close signal (price must have breached the monthly
    Camarilla R3/S3 level, THEN a LATER daily candle must close back
    inside it — a two-step "breach, then confirm" read of recent history,
    not a single condition check against today's price alone)
  - strike selection derived from a LIVE-TRACKED local extreme (the
    fakeout's own low/high over a lookback window), not a fixed points/
    percent/premium offset from ATM
  - a price-LEVEL stop-loss (exit once spot reaches the sold strike),
    not a %-of-premium or %-of-capital one
  - a minimum-ROI entry filter and a manually-maintained earnings-
    blackout date list gating entry

Rules JSON shape::

    {
      "lots": <int> >= 1,
      "expiry_offset": <int> >= 0,        # default 0 — which monthly expiry to trade
                                           #   (0 = nearest listed monthly)
      "extreme_lookback_days": <int> >= 1,# default 10 — trading days scanned back for the
                                           #   fakeout's own extreme low/high once a
                                           #   breach+confirm signal fires
      "hedge_strikes_away": <int> >= 1,   # default 2 — how many strike-steps beyond the
                                           #   sold strike the protective hedge leg sits
      "signal_check_time": "HH:MM",       # default "15:20" — signal is only evaluated
                                           #   at/after this time each day, so today's
                                           #   daily candle is treated as "closed enough"
                                           #   (NSE closes 15:30) rather than still-forming
      "target_credit_pct": <number> > 0,  # default 90 — close once this cycle's
                                           #   cumulative P&L reaches this % of the net
                                           #   credit collected at entry
      "min_roi_pct": <number> > 0,        # default 3 — skip an entry whose max ROI (net
                                           #   credit / max loss) is below this — "poor
                                           #   risk-reward" filter
      "exit_days_before_expiry": <int> >= 0, # default 2 — hard stop, same meaning as
                                           #   everywhere else in this app
      "blackout_dates": [                 # optional, default [] — no earnings-calendar
        {"start": "YYYY-MM-DD",           #   feed exists in this codebase, so these are
         "end": "YYYY-MM-DD"},            #   manually maintained (add each quarter's
        ...                               #   result-date window as it's announced); no
      ]                                   #   new entries while today falls in any window
    }
"""
from datetime import date

_DEFAULTS = {
    "expiry_offset": 0,
    "extreme_lookback_days": 10,
    "hedge_strikes_away": 2,
    "signal_check_time": "15:20",
    "target_credit_pct": 90,
    "min_roi_pct": 3,
    "exit_days_before_expiry": 2,
    "blackout_dates": [],
}


def get_setting(rules: dict, key: str):
    return rules.get(key, _DEFAULTS.get(key))


def _is_hhmm(value) -> bool:
    return isinstance(value, str) and len(value) == 5 and value[2] == ":"


def _is_yyyymmdd(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        date.fromisoformat(value)
        return True
    except ValueError:
        return False


def validate_gravity_rules(rules: dict) -> list[str]:
    """Return a list of human-readable error strings; empty list = valid. Mirrors rule_schema.validate_rules()'s contract."""
    errors: list[str] = []
    if not isinstance(rules, dict):
        return ["Strategy rules must be an object."]

    lots = rules.get("lots", 1)
    if not isinstance(lots, int) or lots < 1:
        errors.append("'lots' must be a positive whole number.")

    expiry_offset = get_setting(rules, "expiry_offset")
    if not isinstance(expiry_offset, int) or expiry_offset < 0:
        errors.append("'expiry_offset' must be a whole number >= 0.")

    for key in ("extreme_lookback_days", "hedge_strikes_away"):
        val = get_setting(rules, key)
        if not isinstance(val, int) or val < 1:
            errors.append(f"'{key}' must be a positive whole number.")

    if not _is_hhmm(get_setting(rules, "signal_check_time")):
        errors.append("'signal_check_time' must be an 'HH:MM' time string.")

    for key in ("target_credit_pct", "min_roi_pct"):
        val = get_setting(rules, key)
        if not isinstance(val, (int, float)) or val <= 0:
            errors.append(f"'{key}' must be a positive number.")

    exit_days = get_setting(rules, "exit_days_before_expiry")
    if not isinstance(exit_days, int) or exit_days < 0:
        errors.append("'exit_days_before_expiry' must be a whole number >= 0.")

    blackout = get_setting(rules, "blackout_dates")
    if not isinstance(blackout, list):
        errors.append("'blackout_dates' must be a list.")
    else:
        for i, window in enumerate(blackout):
            if not isinstance(window, dict) or not _is_yyyymmdd(window.get("start")) or not _is_yyyymmdd(window.get("end")):
                errors.append(f"'blackout_dates[{i}]' must be an object with 'start'/'end' as 'YYYY-MM-DD' dates.")
            elif window["start"] > window["end"]:
                errors.append(f"'blackout_dates[{i}].start' must not be after its 'end'.")

    return errors


def describe_gravity_rules(rules: dict | None, symbol: str = "") -> str:
    """Plain-English summary — same role as rule_schema.describe_rules() for the leg-based shape."""
    if not rules:
        return "No rules configured yet."
    lots = rules.get("lots", 1)
    lookback = get_setting(rules, "extreme_lookback_days")
    hedge_away = get_setting(rules, "hedge_strikes_away")
    check_time = get_setting(rules, "signal_check_time")
    target = get_setting(rules, "target_credit_pct")
    min_roi = get_setting(rules, "min_roi_pct")
    exit_days = get_setting(rules, "exit_days_before_expiry")
    subject = f"{symbol} " if symbol else ""
    return (
        f"SELL {lots} lot{'s' if lots != 1 else ''} {subject}OTM credit spread (2-leg, hedge {hedge_away} strikes "
        f"beyond the sold strike) when a daily candle closes back inside the monthly Camarilla R3/S3 after "
        f"breaching it (checked at/after {check_time}) — sold strike near the fakeout's own {lookback}-day "
        f"extreme. Skips entries under {min_roi}% max ROI or inside a configured earnings blackout window. Exits "
        f"on capturing {target}% of the entry credit, spot reaching the sold strike, or {exit_days} day(s) "
        f"before expiry."
    )
