"""
strategies/custom/session_seller_schema.py — the rule contract for the
"Intraday Nifty & Sensex Option Selling" dual-session strategy. See
session_seller_strategy.py for the executor and api/session_seller_engine.py
for the tick function (registered in api/strategy_scheduler.py's dispatch
— see engine_registry.py for the matching route-layer registration).

A SEVENTH separate engine — none of the following has any equivalent in
the leg-based engine's single "resolve legs once, enter once, exit once"
model:
  - TWO independent sessions in ONE trading day (morning short legs,
    force-closed at a fixed time; a SEPARATE pair of afternoon short legs
    entered fresh, reusing the SAME morning hedge legs for margin) —
    the leg-based engine has no concept of "enter again, same day,
    different strikes."
  - a DAY-OF-WEEK symbol schedule (NIFTY on Mon/Tue/Fri, SENSEX on
    Wed/Thu) — a single strategy trading TWO underlyings on a rotating
    calendar, never both on the same day.
  - PER-LEG stop-loss with explicit NO-RE-ENTRY (if one short leg hits
    its 50% SL, the other stays open and the SL'd leg is never replaced
    until the next scheduled entry) — the leg-based engine's combined
    exit either manages a leg individually or as one group, never with a
    "closed but permanently done for this session" state.

Rules JSON shape::

    {
      "symbol_schedule": {                  # weekday -> which ONE symbol trades that day;
        "MON": "NIFTY", "TUE": "NIFTY",     #   default matches the reference strategy exactly
        "WED": "SENSEX", "THU": "SENSEX",   #   (Sat/Sun and any weekday omitted here = no trading)
        "FRI": "NIFTY"
      },
      "sessions": {                         # per-symbol session clock — default matches the
        "NIFTY": {                          #   reference strategy's exact times
          "morning_entry": "09:16", "morning_exit": "14:15",
          "afternoon_entry": "14:16", "afternoon_exit": "15:28"
        },
        "SENSEX": {
          "morning_entry": "09:16", "morning_exit": "13:30",
          "afternoon_entry": "13:35", "afternoon_exit": "15:28"
        }
      },
      "otm_points": <int> >= 1,             # default 100 — morning short strikes sit this far
                                             #   from ATM on both sides (ATM+otm_points CE,
                                             #   ATM-otm_points PE); afternoon shorts are plain ATM
      "hedge_premium_min": <number> > 0,    # default 1 — live-premium band (₹) the cheap
      "hedge_premium_max": <number> > 0,    #   morning hedge legs are searched for, outward
                                             #   from ATM (see PREMIUM_BAND in rule_schema.py —
                                             #   same search discipline, different engine)
      "lots": <int> >= 1,
      "stop_loss_pct": <number> > 0,        # default 50 — PER LEG, % of that leg's own entry
                                             #   premium; hit -> close ONLY that leg, no re-entry
                                             #   this session (the other leg keeps running)
      "blackout_dates": [                   # optional, default [] — no economic-calendar feed
        {"start": "YYYY-MM-DD",             #   exists in this codebase (same as gravity_schema.py),
         "end": "YYYY-MM-DD"},              #   so these are manually maintained (Budget day,
        ...                                 #   election results day, etc.) — no new entries while
      ]                                     #   today falls in any window
    }
"""
from datetime import date

_WEEKDAYS = {"MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"}
_SYMBOLS = {"NIFTY", "SENSEX"}

_DEFAULT_SCHEDULE = {"MON": "NIFTY", "TUE": "NIFTY", "WED": "SENSEX", "THU": "SENSEX", "FRI": "NIFTY"}
_DEFAULT_SESSIONS = {
    "NIFTY": {"morning_entry": "09:16", "morning_exit": "14:15", "afternoon_entry": "14:16", "afternoon_exit": "15:28"},
    "SENSEX": {"morning_entry": "09:16", "morning_exit": "13:30", "afternoon_entry": "13:35", "afternoon_exit": "15:28"},
}
_DEFAULTS = {
    "symbol_schedule": _DEFAULT_SCHEDULE,
    "sessions": _DEFAULT_SESSIONS,
    "otm_points": 100,
    "hedge_premium_min": 1,
    "hedge_premium_max": 2,
    "stop_loss_pct": 50,
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


def validate_session_seller_rules(rules: dict) -> list[str]:
    """Return a list of human-readable error strings; empty list = valid. Mirrors rule_schema.validate_rules()'s contract."""
    errors: list[str] = []
    if not isinstance(rules, dict):
        return ["Strategy rules must be an object."]

    lots = rules.get("lots", 1)
    if not isinstance(lots, int) or lots < 1:
        errors.append("'lots' must be a positive whole number.")

    schedule = get_setting(rules, "symbol_schedule")
    if not isinstance(schedule, dict):
        errors.append("'symbol_schedule' must be an object.")
    else:
        for wd, sym in schedule.items():
            if wd not in _WEEKDAYS:
                errors.append(f"'symbol_schedule' key '{wd}' must be one of {sorted(_WEEKDAYS)}.")
            if sym not in _SYMBOLS:
                errors.append(f"'symbol_schedule.{wd}' must be one of {sorted(_SYMBOLS)}.")

    sessions = get_setting(rules, "sessions")
    if not isinstance(sessions, dict):
        errors.append("'sessions' must be an object.")
    else:
        for sym, cfg in sessions.items():
            if sym not in _SYMBOLS:
                errors.append(f"'sessions' key '{sym}' must be one of {sorted(_SYMBOLS)}.")
                continue
            if not isinstance(cfg, dict):
                errors.append(f"'sessions.{sym}' must be an object.")
                continue
            for key in ("morning_entry", "morning_exit", "afternoon_entry", "afternoon_exit"):
                if not _is_hhmm(cfg.get(key)):
                    errors.append(f"'sessions.{sym}.{key}' must be an 'HH:MM' time string.")

    if not isinstance(get_setting(rules, "otm_points"), int) or get_setting(rules, "otm_points") < 1:
        errors.append("'otm_points' must be a positive whole number.")

    for key in ("hedge_premium_min", "hedge_premium_max", "stop_loss_pct"):
        val = get_setting(rules, key)
        if not isinstance(val, (int, float)) or val <= 0:
            errors.append(f"'{key}' must be a positive number.")

    hedge_min, hedge_max = get_setting(rules, "hedge_premium_min"), get_setting(rules, "hedge_premium_max")
    if isinstance(hedge_min, (int, float)) and isinstance(hedge_max, (int, float)) and hedge_min > hedge_max:
        errors.append("'hedge_premium_min' must not be greater than 'hedge_premium_max'.")

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


def describe_session_seller_rules(rules: dict | None, symbol: str = "") -> str:
    """Plain-English summary — same role as rule_schema.describe_rules() for the leg-based shape. `symbol` is ignored — this strategy trades whichever symbol its own schedule picks, not a fixed one."""
    if not rules:
        return "No rules configured yet."
    lots = rules.get("lots", 1)
    schedule = get_setting(rules, "symbol_schedule")
    otm = get_setting(rules, "otm_points")
    sl = get_setting(rules, "stop_loss_pct")
    hedge_min, hedge_max = get_setting(rules, "hedge_premium_min"), get_setting(rules, "hedge_premium_max")
    by_symbol: dict[str, list[str]] = {}
    for wd, sym in schedule.items():
        by_symbol.setdefault(sym, []).append(wd)
    schedule_txt = "; ".join(f"{sym} on {', '.join(days)}" for sym, days in by_symbol.items())
    return (
        f"SELL {lots} lot{'s' if lots != 1 else ''} dual-session intraday strangle — {schedule_txt}. Each trading "
        f"day: buy ₹{hedge_min}-{hedge_max} OTM hedges + SELL {otm}pt-OTM CE/PE in the morning session, force-exit "
        f"the shorts at that symbol's morning cutoff, then SELL fresh ATM CE/PE in the afternoon reusing the same "
        f"hedges, force-exit everything (shorts + hedges) by the afternoon cutoff — never held overnight. Each "
        f"short leg has its own {sl}% stop-loss; a leg that's stopped out is NOT re-entered that session, the "
        f"other leg keeps running. Skips entries inside a configured event-day blackout window."
    )
