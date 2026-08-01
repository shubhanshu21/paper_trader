"""
strategies/custom/rule_schema.py — the composable rule contract that backs
every user-built custom strategy.

Scope: OPTIONS strategies only (index/stock/commodity options — no
equity/future legs). A strategy is not a fixed template (STRADDLE/
STRANGLE/...) — it is any number of option "legs" (buy/sell, CE/PE, how
to pick the strike) plus one entry rule and one exit rule. Any options
strategy a user can describe in Streak-style plain English (e.g. "Sell 1
lot NIFTY 5% OTM CE and 1 lot NIFTY 5% OTM PE, enter at 9:20am, exit at
20% loss or 40% profit, always square off 1 day before expiry") is just
one value of this shape — that's what makes this genuinely general
instead of a menu of five presets.

Every strategy a user actually builds is stored as a row in the
custom_strategies table (rules_json column, see db/models.py) — nothing
user-specific lives in a file. This module only defines/validates the
shape of that JSON and is shared code, not per-strategy data.

The wizard UI (frontend/src/components/StrategyBuilderModal.tsx /
StrategyFlowCanvas.tsx) only ever presents dropdowns/plain-English rows
built from this shape — the user never sees or edits this JSON directly.

Rules JSON shape::

    {
      "legs": [
        {
          "action": "BUY" | "SELL",
          "option_type": "CE" | "PE",
          "strike_selection": {
            "mode": "ATM" | "OTM_PERCENT" | "OTM_POINTS" | "FIXED",
            "value": <number> | null                # null only for ATM
          },
          "lots": <int> >= 1,
          "sizing": {                                # optional — omitted/LOTS = today's fixed-lots behavior
            "mode": "LOTS" | "RISK_PCT",
            "risk_pct": <number> 0 < x <= 100         # required iff RISK_PCT — size this leg so its
          } | null                                    #   margin/premium <= risk_pct% of available capital
          "expiry_mode": "WEEKLY" | "MONTHLY" | null, # optional — omitted = use the strategy's expiry.mode
                                                       #   (a leg overriding this is what makes a calendar
                                                       #   spread possible — see rule_strategy.py)
          "exit": {                                   # optional — omitted = this leg only exits via the
            "take_profit_pct": <number> | null,       #   strategy-level combined exit below (today's
            "stop_loss_pct": <number> | null,          #   only behavior)
            "trailing": {
              "enabled": <bool>,
              "trail_amount": <number> > 0,
              "trail_type": "points" | "percentage"
            } | null
          } | null
        },
        ...
      ],
      "entry": {
        "mode": "IMMEDIATE" | "AT_TIME" | "CONDITIONAL",
        "time": "HH:MM" | null,                      # required iff AT_TIME
        "condition": {                                # required iff CONDITIONAL
          "type": "MA_CROSSOVER",
          "period_days": <int> >= 2,
          "direction": "ABOVE" | "BELOW"               # enter when price crosses ABOVE/BELOW its own N-day MA
        } | {
          "type": "IV_RANK",
          "operator": "ABOVE" | "BELOW",
          "threshold": <number> 0-100                  # IV rank over the trailing ~1y window; see
        } | null                                        #   utils/iv_rank.py — returns "not triggered" (never
                                                         #   a fabricated signal) until enough history exists
      },
      "expiry": {
        "mode": "WEEKLY" | "MONTHLY"                 # optional, defaults to WEEKLY — the STRATEGY default;
      },                                              # a leg's own expiry_mode above overrides it for that leg
      "exit": {
        "take_profit_pct": <number> | null,
        "stop_loss_pct": <number> | null,
        "exit_time": "HH:MM" | null,
        "exit_days_before_expiry": <int> >= 0
      }
    }

take_profit_pct/stop_loss_pct at the STRATEGY level are evaluated
against the COMBINED premium across every leg that doesn't have its own
leg-level `exit` (matches the existing strangle's strangle_pnl_pct
convention — a positive number for each means "the combined position is
worth this % less than what was collected/paid at entry",
direction-agnostic across BUY/SELL legs). A leg WITH its own `exit` is
excluded from that combined check and managed independently instead —
see custom_strategy_scheduler.py::_try_exit / backtest/custom_engine.py.
"""
from typing import List, Optional

_ACTIONS = {"BUY", "SELL"}
_OPTION_TYPES = {"CE", "PE"}
_STRIKE_MODES = {"ATM", "OTM_PERCENT", "OTM_POINTS", "FIXED"}
_ENTRY_MODES = {"IMMEDIATE", "AT_TIME", "CONDITIONAL"}
_EXPIRY_MODES = {"WEEKLY", "MONTHLY"}
_SIZING_MODES = {"LOTS", "RISK_PCT"}
_TRAIL_TYPES = {"points", "percentage"}
_CONDITION_TYPES = {"MA_CROSSOVER", "IV_RANK"}
_MA_DIRECTIONS = {"ABOVE", "BELOW"}
_IV_RANK_OPERATORS = {"ABOVE", "BELOW"}

MAX_LEGS = 8


def _is_hhmm(value) -> bool:
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        return False
    h, m = value[:2], value[3:]
    return h.isdigit() and m.isdigit() and 0 <= int(h) <= 23 and 0 <= int(m) <= 59


def _validate_leg_exit(leg_exit, prefix: str) -> List[str]:
    """Optional per-leg exit block — same shape/rules as the strategy-level exit, plus optional trailing."""
    errors: List[str] = []
    if leg_exit is None:
        return errors
    if not isinstance(leg_exit, dict):
        return [f"{prefix} exit must be an object, or omitted."]
    for key in ("take_profit_pct", "stop_loss_pct"):
        val = leg_exit.get(key)
        if val is not None and (not isinstance(val, (int, float)) or val <= 0):
            errors.append(f"{prefix} exit '{key}' must be a positive number, or left blank to disable it.")
    trailing = leg_exit.get("trailing")
    if trailing is not None:
        if not isinstance(trailing, dict):
            errors.append(f"{prefix} trailing must be an object, or omitted.")
        else:
            enabled = trailing.get("enabled")
            if not isinstance(enabled, bool):
                errors.append(f"{prefix} trailing 'enabled' must be true or false.")
            if enabled:
                amount = trailing.get("trail_amount")
                if not isinstance(amount, (int, float)) or amount <= 0:
                    errors.append(f"{prefix} trailing 'trail_amount' must be a positive number.")
                if trailing.get("trail_type") not in _TRAIL_TYPES:
                    errors.append(f"{prefix} trailing 'trail_type' must be one of {sorted(_TRAIL_TYPES)}.")
    return errors


def _validate_leg_sizing(sizing, prefix: str) -> List[str]:
    """Optional per-leg sizing override — omitted/LOTS keeps today's fixed-`lots` behavior."""
    if sizing is None:
        return []
    if not isinstance(sizing, dict) or sizing.get("mode") not in _SIZING_MODES:
        return [f"{prefix} sizing mode must be one of {sorted(_SIZING_MODES)}, or omitted."]
    if sizing["mode"] == "RISK_PCT":
        risk_pct = sizing.get("risk_pct")
        if not isinstance(risk_pct, (int, float)) or not (0 < risk_pct <= 100):
            return [f"{prefix} sizing 'risk_pct' must be a number between 0 (exclusive) and 100."]
    return []


def _validate_entry_condition(entry: dict) -> List[str]:
    condition = entry.get("condition")
    if not isinstance(condition, dict) or condition.get("type") not in _CONDITION_TYPES:
        return [f"Entry condition type must be one of {sorted(_CONDITION_TYPES)}."]
    if condition["type"] == "MA_CROSSOVER":
        period = condition.get("period_days")
        if not isinstance(period, int) or period < 2:
            return ["MA crossover 'period_days' must be a whole number >= 2."]
        if condition.get("direction") not in _MA_DIRECTIONS:
            return [f"MA crossover 'direction' must be one of {sorted(_MA_DIRECTIONS)}."]
    elif condition["type"] == "IV_RANK":
        if condition.get("operator") not in _IV_RANK_OPERATORS:
            return [f"IV rank 'operator' must be one of {sorted(_IV_RANK_OPERATORS)}."]
        threshold = condition.get("threshold")
        if not isinstance(threshold, (int, float)) or not (0 <= threshold <= 100):
            return ["IV rank 'threshold' must be a number between 0 and 100."]
    return []


def validate_rules(rules: dict) -> List[str]:
    """Return a list of human-readable error strings; empty list = valid."""
    errors: List[str] = []
    if not isinstance(rules, dict):
        return ["Strategy rules must be an object."]

    legs = rules.get("legs")
    if not isinstance(legs, list) or not legs:
        errors.append("Add at least one leg (what to buy or sell).")
    elif len(legs) > MAX_LEGS:
        errors.append(f"A strategy can have at most {MAX_LEGS} legs.")
    else:
        seen_leg_signatures: dict = {}
        for i, leg in enumerate(legs, 1):
            prefix = f"Leg {i}:"
            if not isinstance(leg, dict):
                errors.append(f"{prefix} must be an object.")
                continue
            action = leg.get("action")
            if action not in _ACTIONS:
                errors.append(f"{prefix} action must be BUY or SELL.")
            lots = leg.get("lots", 1)
            if not isinstance(lots, int) or lots < 1:
                errors.append(f"{prefix} lots must be a positive whole number.")
            if leg.get("option_type") not in _OPTION_TYPES:
                errors.append(f"{prefix} option type must be CE or PE.")
            sel = leg.get("strike_selection")
            if not isinstance(sel, dict) or sel.get("mode") not in _STRIKE_MODES:
                errors.append(f"{prefix} strike selection must be one of {sorted(_STRIKE_MODES)}.")
            elif sel["mode"] != "ATM":
                val = sel.get("value")
                if not isinstance(val, (int, float)):
                    errors.append(f"{prefix} strike selection '{sel['mode']}' needs a numeric value.")
                elif sel["mode"] in ("OTM_PERCENT", "OTM_POINTS") and val < 0:
                    errors.append(f"{prefix} OTM distance can't be negative.")

            expiry_mode = leg.get("expiry_mode")
            if expiry_mode is not None and expiry_mode not in _EXPIRY_MODES:
                errors.append(f"{prefix} expiry_mode must be one of {sorted(_EXPIRY_MODES)}, or omitted.")

            errors.extend(_validate_leg_exit(leg.get("exit"), prefix))
            errors.extend(_validate_leg_sizing(leg.get("sizing"), prefix))

            # Two legs with identical (action, option_type, strike, expiry_mode)
            # are always a mistake — the user meant a higher `lots` on ONE
            # leg, not two separate legs that resolve to the exact same
            # instrument. Only checked once every other field on this leg
            # is already known-valid, so a signature is never built from
            # malformed data. expiry_mode is part of the signature now —
            # two legs at the SAME strike but DIFFERENT expiries (a
            # calendar spread) are legitimately different instruments,
            # not a duplicate.
            if action in _ACTIONS and leg.get("option_type") in _OPTION_TYPES and isinstance(sel, dict) and sel.get("mode") in _STRIKE_MODES:
                value = sel.get("value")
                signature = (action, leg["option_type"], sel["mode"], float(value) if isinstance(value, (int, float)) else None, expiry_mode)
                if signature in seen_leg_signatures:
                    errors.append(f"{prefix} identical to Leg {seen_leg_signatures[signature]} — combine them into one leg with a higher lot count instead.")
                else:
                    seen_leg_signatures[signature] = i

    entry = rules.get("entry")
    if not isinstance(entry, dict) or entry.get("mode") not in _ENTRY_MODES:
        errors.append(f"Entry mode must be one of {sorted(_ENTRY_MODES)}.")
    elif entry["mode"] == "AT_TIME" and not _is_hhmm(entry.get("time")):
        errors.append("Entry time must be in HH:MM format.")
    elif entry["mode"] == "CONDITIONAL":
        errors.extend(_validate_entry_condition(entry))

    expiry = rules.get("expiry")
    if expiry is not None and (not isinstance(expiry, dict) or expiry.get("mode") not in _EXPIRY_MODES):
        errors.append(f"Expiry mode must be one of {sorted(_EXPIRY_MODES)} (or omitted — defaults to WEEKLY).")

    exit_ = rules.get("exit")
    if not isinstance(exit_, dict):
        errors.append("Exit rules are required (even if everything is left blank/disabled).")
    else:
        for key in ("take_profit_pct", "stop_loss_pct"):
            val = exit_.get(key)
            if val is not None and (not isinstance(val, (int, float)) or val <= 0):
                errors.append(f"Exit '{key}' must be a positive number, or left blank to disable it.")
        exit_time = exit_.get("exit_time")
        if exit_time is not None and not _is_hhmm(exit_time):
            errors.append("Exit time must be in HH:MM format.")
        exit_days = exit_.get("exit_days_before_expiry", 0)
        if not isinstance(exit_days, int) or exit_days < 0:
            errors.append("'Exit days before expiry' must be a whole number >= 0.")

    return errors


def _strike_phrase(leg: dict) -> str:
    sel = leg.get("strike_selection") or {}
    mode = sel.get("mode")
    if mode == "ATM":
        return "ATM (at-the-money)"
    if mode == "OTM_PERCENT":
        return f"{sel.get('value')}% OTM"
    if mode == "OTM_POINTS":
        return f"{sel.get('value')} points OTM"
    if mode == "FIXED":
        return f"strike {sel.get('value')}"
    return "an unspecified strike"


def _leg_phrase(leg: dict) -> str:
    action = leg.get("action", "?")
    lots = leg.get("lots", 1)
    sizing = leg.get("sizing") or {}
    size_txt = f"risking {sizing['risk_pct']}% of capital" if sizing.get("mode") == "RISK_PCT" else f"{lots} {'lot' if lots == 1 else 'lots'}"
    phrase = f"{action} {size_txt} {_strike_phrase(leg)} {leg.get('option_type')}"
    if leg.get("expiry_mode"):
        phrase += f" ({leg['expiry_mode'].lower()} expiry)"
    return phrase


def describe_rules(rules: Optional[dict], symbol: str = "") -> str:
    """
    Render a strategy's rules as one plain-English sentence, the same style
    Streak uses ("IF ... THEN ..."). Shown in the strategy list/detail view
    and used as the auto-generated description when the user leaves the
    free-text description blank.
    """
    if not rules or not rules.get("legs"):
        return "No rules configured yet."

    legs_txt = " + ".join(_leg_phrase(leg) for leg in rules["legs"])
    subject = f"{symbol} " if symbol else ""
    sentence = f"{legs_txt} on {subject}".rstrip() if symbol else legs_txt

    expiry_mode = (rules.get("expiry") or {}).get("mode", "WEEKLY")
    sentence += f" ({expiry_mode.lower()} expiry default)"

    entry = rules.get("entry") or {}
    if entry.get("mode") == "AT_TIME" and entry.get("time"):
        sentence += f", enter at {entry['time']}"
    elif entry.get("mode") == "CONDITIONAL" and entry.get("condition"):
        c = entry["condition"]
        if c.get("type") == "MA_CROSSOVER":
            sentence += f", enter when price crosses {c.get('direction', '?').lower()} its {c.get('period_days')}-day average"
        elif c.get("type") == "IV_RANK":
            sentence += f", enter when IV rank is {c.get('operator', '?').lower()} {c.get('threshold')}"
    else:
        sentence += ", enter immediately when the strategy goes live"

    exit_ = rules.get("exit") or {}
    exit_bits = []
    if exit_.get("take_profit_pct"):
        exit_bits.append(f"+{exit_['take_profit_pct']}% profit")
    if exit_.get("stop_loss_pct"):
        exit_bits.append(f"-{exit_['stop_loss_pct']}% loss")
    if exit_.get("exit_time"):
        exit_bits.append(f"{exit_['exit_time']} time exit")
    if exit_.get("exit_days_before_expiry"):
        d = exit_["exit_days_before_expiry"]
        exit_bits.append(f"{d} day{'s' if d != 1 else ''} before expiry")
    sentence += ", exit on " + (" or ".join(exit_bits) if exit_bits else "expiry only")
    if any(leg.get("exit") for leg in rules["legs"]):
        sentence += " (some legs have their own independent exit rules)"
    return sentence + "."
