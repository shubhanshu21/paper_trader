"""
strategies/custom/rule_schema.py — the composable rule contract that backs
every user-built custom strategy.

Every leg is either an OPTION (index/stock/commodity options — the
original, still-default scope) or an EQUITY (cash BUY/SELL of the
underlying itself, no strike/expiry/option_type) — see leg.instrument_type
below. A strategy is not a fixed template (STRADDLE/STRANGLE/...) — it is
any number of legs (buy/sell, CE/PE, how to pick the strike, or a plain
equity leg) plus one entry rule and one exit rule. Any options strategy a
user can describe in Streak-style plain English (e.g. "Sell 1 lot NIFTY
5% OTM CE and 1 lot NIFTY 5% OTM PE, enter at 9:20am, exit at 20% loss or
40% profit, always square off 1 day before expiry") is just one value of
this shape — that's what makes this genuinely general instead of a menu
of five presets.

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
          "instrument_type": "OPTION" | "EQUITY" | null,  # optional, defaults to OPTION (backward compat)
          "action": "BUY" | "SELL",
          "option_type": "CE" | "PE",                # required iff instrument_type == OPTION
          "strike_selection": {                       # required iff instrument_type == OPTION
            "mode": "ATM" | "OTM_PERCENT" | "OTM_POINTS" | "FIXED" | "PREMIUM_OFFSET" | "PREMIUM_BAND",
            "value": <number> | null,               # null only for ATM/PREMIUM_BAND
                                                       #   PREMIUM_OFFSET: divisor applied to the ATM
                                                       #   straddle's live premium (see below)
            "min": <number> | null,                 # PREMIUM_BAND only — premium band lower bound (₹)
            "max": <number> | null                  # PREMIUM_BAND only — premium band upper bound (₹)
          },
          "lots": <int> >= 1,                        # for EQUITY: raw share quantity, not an F&O lot count
                                                       # (see rule_strategy.py's real_lot_size=1 for equity legs)
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
        "mode": "IMMEDIATE" | "AT_TIME" | "CONDITIONAL" | "BEFORE_EXPIRY",
        "time": "HH:MM" | null,                      # required iff AT_TIME
        "weekday": "MON".."SUN" | null,               # AT_TIME only, optional — restricts entry to this ONE
                                                       #   weekday (a HARD gate, unlike BEFORE_EXPIRY.weekday's
                                                       #   soft holiday-fallback preference — AT_TIME has no
                                                       #   expiry-relative "last chance" window to fall back to,
                                                       #   so a wrong/missed weekday just waits for next cycle).
                                                       #   Omitted = every day, today's original behavior.
        "condition": {                                # required iff CONDITIONAL
          "type": "MA_CROSSOVER",
          "period_days": <int> >= 2,
          "direction": "ABOVE" | "BELOW"               # enter when price crosses ABOVE/BELOW its own N-day MA
        } | {
          "type": "IV_RANK",
          "operator": "ABOVE" | "BELOW",
          "threshold": <number> 0-100                  # IV rank over the trailing ~1y window; see
        } | null,                                       #   utils/iv_rank.py — returns "not triggered" (never
                                                         #   a fabricated signal) until enough history exists
        "before_expiry": {                              # required iff BEFORE_EXPIRY — "enter close to expiry"
          "days_before_expiry": <int> >= 1,             #   instead of right after the PREVIOUS expiry rolls
          "weekday": "MON".."SUN" | null,               #   over (which is what AT_TIME alone would do). Window
          "time": "HH:MM" | null                        #   opens `days_before_expiry` calendar days before the
        } | null                                        #   resolved expiry (utils/option_utils.py's
                                                         #   is_within_pre_expiry_buffer); `weekday` is a soft
                                                         #   preference — see custom_strategy_scheduler.py's
                                                         #   _before_expiry_eligible for why it's never allowed
                                                         #   to silently skip an entire monthly cycle over a
                                                         #   holiday landing on the target weekday
      },
      "expiry": {
        "mode": "WEEKLY" | "MONTHLY",                # optional, defaults to WEEKLY — the STRATEGY default;
                                                       #   a leg's own expiry_mode above overrides it for that leg
        "expiry_offset": <int> >= 0 | null            # optional, defaults to 0 (the nearest expiry of `mode`) —
                                                       #   1 = skip the nearest and trade the one after that
                                                       #   instead (e.g. "next week's" contract entered every
                                                       #   Monday, to avoid an unintentionally short-DTE entry).
                                                       #   Only applied to legs using the strategy's default
                                                       #   mode — a leg with its own expiry_mode override
                                                       #   (calendar spread) always resolves offset 0.
      },

      "product": "DELIVERY" | "INTRADAY",             # optional, defaults to "DELIVERY" — applies to EVERY
                                                       #   leg in the strategy (options AND equity), not
                                                       #   configurable per-leg. "DELIVERY" places NRML orders
                                                       #   (today's original behavior — holds across days/
                                                       #   weeks, the only sensible choice for anything with a
                                                       #   real expiry). "INTRADAY" places MIS orders (lower
                                                       #   margin, mandatory same-day square-off) AND adds an
                                                       #   unconditional hard exit at
                                                       #   INTRADAY_SQUAREOFF_TIME (custom_strategy_scheduler.py)
                                                       #   regardless of exit_time/take_profit/stop_loss state —
                                                       #   this strategy shape NEVER holds a position overnight,
                                                       #   same guarantee SUPERTREND_INTRADAY gives, generalized
                                                       #   to any leg combination (most usefully EQUITY legs,
                                                       #   which otherwise have no forced same-day exit at all).

      "exit": {
        "take_profit_pct": <number> | null,
        "stop_loss_pct": <number> | null,
        "take_profit_amount": <number> | null,      # absolute ₹ combined MTM profit — checked ALONGSIDE
                                                       #   take_profit_pct (either firing exits); rupee terms
                                                       #   don't scale with premium the way a % target does
        "take_profit_capital_pct": <number> | null, # % of REAL broker margin blocked for this basket at
        "stop_loss_capital_pct": <number> | null,   #   entry (broker.get_basket_required_margin, snapshotted
                                                       #   once at entry — see custom_strategy_scheduler.py's
                                                       #   _try_entry/_get_last_entered_margin), NOT % of
                                                       #   premium — the right base for a margin-heavy net-
                                                       #   credit structure where premium collected is a small
                                                       #   fraction of capital actually deployed. Checked
                                                       #   ALONGSIDE take_profit_pct/stop_loss_pct (whichever
                                                       #   fires first wins) — never fires if this basket's
                                                       #   broker doesn't support basket margin lookup (e.g.
                                                       #   MockBroker/backtest), same "can't evaluate safely,
                                                       #   don't guess" default as everywhere else here.
        "stop_loss_mode": "PCT" | "BREAKEVEN",       # optional, defaults to "PCT" (today's behavior:
                                                       #   stop_loss_pct against combined premium). "BREAKEVEN"
                                                       #   ignores stop_loss_pct and instead exits once the
                                                       #   underlying's spot crosses outside the position's own
                                                       #   computed breakeven range (see utils/option_utils.py's
                                                       #   find_breakevens — an exact piecewise-linear payoff
                                                       #   root-find over every OPTION leg's strike/entry/qty,
                                                       #   not a fixed distance). stop_loss_capital_pct above is
                                                       #   independent of this switch, same as take_profit_amount.
        "exit_time": "HH:MM" | null,
        "exit_weekday": "MON".."SUN" | null,         # exit_time only, optional — restricts the exit_time hard
                                                       #   stop to this ONE weekday (e.g. "close by 3:15pm
                                                       #   Friday", not every single day at 3:15pm). Omitted =
                                                       #   every day, today's original behavior.
        "exit_days_before_expiry": <int> >= 0
      }
    }

PREMIUM_OFFSET and PREMIUM_BAND are the two strike-selection modes that
read LIVE option premiums at entry time instead of a fixed distance from
spot (see strategies/custom/rule_strategy.py's
_resolve_premium_offset_strike/_resolve_premium_band_strike):

  - PREMIUM_OFFSET: strike = ATM ± round_to_nearest_strike(atm_straddle_
    premium / value, strike_step), where atm_straddle_premium is the SUM
    of the live ATM CE + ATM PE premiums (fetched from the option chain
    regardless of whether the strategy actually has ATM legs of its own)
    and `value` is the divisor (e.g. 2 = "half the straddle premium").
    Classic use: sizing a short strangle's distance off a long straddle's
    own cost, the way many credit-adjustment strategies do it.
  - PREMIUM_BAND: strike = the nearest OTM strike (searching outward from
    ATM in strike_step increments, in that leg's OTM direction) whose own
    live premium falls within [min, max] (₹). Used to target a strike by
    "how much it costs" rather than "how far it is" — e.g. picking a deep
    OTM insurance leg that trades around ₹40-70, a rough proxy for a
    target delta without needing a live delta feed. Raises (refuses to
    guess) if no strike within a bounded search window matches.

take_profit_pct/stop_loss_pct at the STRATEGY level are evaluated
against the COMBINED premium across every leg that doesn't have its own
leg-level `exit` (matches the existing strangle's strangle_pnl_pct
convention — a positive number for each means "the combined position is
worth this % less than what was collected/paid at entry",
direction-agnostic across BUY/SELL legs). A leg WITH its own `exit` is
excluded from that combined check and managed independently instead —
see custom_strategy_scheduler.py::_try_exit / backtest/custom_engine.py.
"""

_ACTIONS = {"BUY", "SELL"}
_LEG_INSTRUMENT_TYPES = {"OPTION", "EQUITY"}
_OPTION_TYPES = {"CE", "PE"}
_STRIKE_MODES = {"ATM", "OTM_PERCENT", "OTM_POINTS", "FIXED", "PREMIUM_OFFSET", "PREMIUM_BAND"}
_PREMIUM_BAND_MODE = "PREMIUM_BAND"
_STOP_LOSS_MODES = {"PCT", "BREAKEVEN"}
_ENTRY_MODES = {"IMMEDIATE", "AT_TIME", "CONDITIONAL", "BEFORE_EXPIRY"}
_EXPIRY_MODES = {"WEEKLY", "MONTHLY"}
_PRODUCT_MODES = {"DELIVERY", "INTRADAY"}
_SIZING_MODES = {"LOTS", "RISK_PCT"}
_TRAIL_TYPES = {"points", "percentage"}
_CONDITION_TYPES = {"MA_CROSSOVER", "IV_RANK"}
_MA_DIRECTIONS = {"ABOVE", "BELOW"}
_IV_RANK_OPERATORS = {"ABOVE", "BELOW"}
_WEEKDAYS = {"MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"}

MAX_LEGS = 8


def _is_hhmm(value) -> bool:
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        return False
    h, m = value[:2], value[3:]
    return h.isdigit() and m.isdigit() and 0 <= int(h) <= 23 and 0 <= int(m) <= 59


def _validate_leg_exit(leg_exit, prefix: str) -> list[str]:
    """Optional per-leg exit block — same shape/rules as the strategy-level exit, plus optional trailing."""
    errors: list[str] = []
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


def _validate_leg_sizing(sizing, prefix: str) -> list[str]:
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


def _validate_entry_condition(entry: dict) -> list[str]:
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


def _validate_before_expiry(entry: dict) -> list[str]:
    before = entry.get("before_expiry")
    if not isinstance(before, dict):
        return ["BEFORE_EXPIRY entry requires a 'before_expiry' object."]
    errors: list[str] = []
    days_before = before.get("days_before_expiry")
    if not isinstance(days_before, int) or days_before < 1:
        errors.append("'before_expiry.days_before_expiry' must be a whole number >= 1.")
    weekday = before.get("weekday")
    if weekday is not None and weekday not in _WEEKDAYS:
        errors.append(f"'before_expiry.weekday' must be one of {sorted(_WEEKDAYS)}, or omitted.")
    time_gate = before.get("time")
    if time_gate is not None and not _is_hhmm(time_gate):
        errors.append("'before_expiry.time' must be in HH:MM format, or omitted.")
    return errors


def validate_rules(rules: dict) -> list[str]:
    """Return a list of human-readable error strings; empty list = valid."""
    errors: list[str] = []
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

            leg_instrument_type = leg.get("instrument_type") or "OPTION"
            if leg_instrument_type not in _LEG_INSTRUMENT_TYPES:
                errors.append(f"{prefix} instrument_type must be one of {sorted(_LEG_INSTRUMENT_TYPES)}, or omitted.")

            expiry_mode = leg.get("expiry_mode")
            sel = None
            if leg_instrument_type == "EQUITY":
                # No option_type/strike/expiry — a cash equity leg is just
                # BUY/SELL N shares of the underlying itself.
                if leg.get("option_type") is not None:
                    errors.append(f"{prefix} EQUITY legs cannot have an option_type.")
                if leg.get("strike_selection") is not None:
                    errors.append(f"{prefix} EQUITY legs cannot have a strike_selection.")
                if expiry_mode is not None:
                    errors.append(f"{prefix} EQUITY legs cannot have an expiry_mode (equity has no expiry).")
            else:
                if leg.get("option_type") not in _OPTION_TYPES:
                    errors.append(f"{prefix} option type must be CE or PE.")
                sel = leg.get("strike_selection")
                if not isinstance(sel, dict) or sel.get("mode") not in _STRIKE_MODES:
                    errors.append(f"{prefix} strike selection must be one of {sorted(_STRIKE_MODES)}.")
                elif sel["mode"] == _PREMIUM_BAND_MODE:
                    band_min, band_max = sel.get("min"), sel.get("max")
                    if not isinstance(band_min, (int, float)) or band_min <= 0:
                        errors.append(f"{prefix} PREMIUM_BAND 'min' must be a positive number (₹).")
                    if not isinstance(band_max, (int, float)) or band_max <= 0:
                        errors.append(f"{prefix} PREMIUM_BAND 'max' must be a positive number (₹).")
                    if isinstance(band_min, (int, float)) and isinstance(band_max, (int, float)) and band_min > band_max:
                        errors.append(f"{prefix} PREMIUM_BAND 'min' can't be greater than 'max'.")
                elif sel["mode"] != "ATM":
                    val = sel.get("value")
                    if not isinstance(val, (int, float)):
                        errors.append(f"{prefix} strike selection '{sel['mode']}' needs a numeric value.")
                    elif sel["mode"] in ("OTM_PERCENT", "OTM_POINTS") and val < 0:
                        errors.append(f"{prefix} OTM distance can't be negative.")
                    elif sel["mode"] == "PREMIUM_OFFSET" and val <= 0:
                        errors.append(f"{prefix} PREMIUM_OFFSET divisor must be a positive number.")
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
            # not a duplicate. EQUITY legs are matched on (action,
            # "EQUITY") only — two EQUITY BUY legs on the same underlying
            # are always the same instruction, no strike to disambiguate.
            if leg_instrument_type == "EQUITY" and action in _ACTIONS:
                signature = (action, "EQUITY", None, None, None)
                if signature in seen_leg_signatures:
                    errors.append(f"{prefix} identical to Leg {seen_leg_signatures[signature]} — combine them into one leg with a higher lot/share count instead.")
                else:
                    seen_leg_signatures[signature] = i
            elif action in _ACTIONS and leg.get("option_type") in _OPTION_TYPES and isinstance(sel, dict) and sel.get("mode") in _STRIKE_MODES:
                if sel["mode"] == _PREMIUM_BAND_MODE:
                    # No single "value" for PREMIUM_BAND — its band (min, max)
                    # is what actually distinguishes it from another PREMIUM_BAND
                    # leg; two legs targeting different bands are NOT a duplicate
                    # even though they'd otherwise collide on a bare `value` of None.
                    band_min, band_max = sel.get("min"), sel.get("max")
                    extra = (
                        float(band_min) if isinstance(band_min, (int, float)) else None,
                        float(band_max) if isinstance(band_max, (int, float)) else None,
                    )
                else:
                    value = sel.get("value")
                    extra = float(value) if isinstance(value, (int, float)) else None
                signature = (action, leg["option_type"], sel["mode"], extra, expiry_mode)
                if signature in seen_leg_signatures:
                    errors.append(f"{prefix} identical to Leg {seen_leg_signatures[signature]} — combine them into one leg with a higher lot count instead.")
                else:
                    seen_leg_signatures[signature] = i

    entry = rules.get("entry")
    if not isinstance(entry, dict) or entry.get("mode") not in _ENTRY_MODES:
        errors.append(f"Entry mode must be one of {sorted(_ENTRY_MODES)}.")
    elif entry["mode"] == "AT_TIME":
        if not _is_hhmm(entry.get("time")):
            errors.append("Entry time must be in HH:MM format.")
        weekday = entry.get("weekday")
        if weekday is not None and weekday not in _WEEKDAYS:
            errors.append(f"'entry.weekday' must be one of {sorted(_WEEKDAYS)}, or omitted.")
    elif entry["mode"] == "CONDITIONAL":
        errors.extend(_validate_entry_condition(entry))
        condition_type = (entry.get("condition") or {}).get("type")
        if condition_type == "IV_RANK" and isinstance(legs, list) and legs and all((leg.get("instrument_type") or "OPTION") == "EQUITY" for leg in legs if isinstance(leg, dict)):
            errors.append("Entry condition 'IV_RANK' doesn't apply to an all-EQUITY strategy (equity has no options IV) — use MA_CROSSOVER instead.")
    elif entry["mode"] == "BEFORE_EXPIRY":
        errors.extend(_validate_before_expiry(entry))
        if isinstance(legs, list) and legs and all((leg.get("instrument_type") or "OPTION") == "EQUITY" for leg in legs if isinstance(leg, dict)):
            errors.append("Entry mode 'BEFORE_EXPIRY' doesn't apply to an all-EQUITY strategy (equity has no expiry) — use IMMEDIATE, AT_TIME, or CONDITIONAL instead.")

    expiry = rules.get("expiry")
    if expiry is not None and (not isinstance(expiry, dict) or expiry.get("mode") not in _EXPIRY_MODES):
        errors.append(f"Expiry mode must be one of {sorted(_EXPIRY_MODES)} (or omitted — defaults to WEEKLY).")
    if isinstance(expiry, dict) and expiry.get("expiry_offset") is not None:
        offset = expiry["expiry_offset"]
        if not isinstance(offset, int) or offset < 0:
            errors.append("'expiry.expiry_offset' must be a whole number >= 0, or omitted.")

    product = rules.get("product")
    if product is not None and product not in _PRODUCT_MODES:
        errors.append(f"'product' must be one of {sorted(_PRODUCT_MODES)}, or omitted (defaults to DELIVERY).")

    exit_ = rules.get("exit")
    if not isinstance(exit_, dict):
        errors.append("Exit rules are required (even if everything is left blank/disabled).")
    else:
        for key in ("take_profit_pct", "stop_loss_pct", "take_profit_amount", "take_profit_capital_pct", "stop_loss_capital_pct"):
            val = exit_.get(key)
            if val is not None and (not isinstance(val, (int, float)) or val <= 0):
                errors.append(f"Exit '{key}' must be a positive number, or left blank to disable it.")
        stop_loss_mode = exit_.get("stop_loss_mode", "PCT")
        if stop_loss_mode not in _STOP_LOSS_MODES:
            errors.append(f"Exit 'stop_loss_mode' must be one of {sorted(_STOP_LOSS_MODES)}, or omitted.")
        exit_time = exit_.get("exit_time")
        if exit_time is not None and not _is_hhmm(exit_time):
            errors.append("Exit time must be in HH:MM format.")
        exit_weekday = exit_.get("exit_weekday")
        if exit_weekday is not None and exit_weekday not in _WEEKDAYS:
            errors.append(f"'exit.exit_weekday' must be one of {sorted(_WEEKDAYS)}, or omitted.")
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
    if mode == "PREMIUM_OFFSET":
        return f"ATM straddle premium / {sel.get('value')} OTM"
    if mode == "PREMIUM_BAND":
        return f"₹{sel.get('min')}-₹{sel.get('max')} premium band"
    return "an unspecified strike"


def _leg_phrase(leg: dict) -> str:
    action = leg.get("action", "?")
    lots = leg.get("lots", 1)
    sizing = leg.get("sizing") or {}
    if (leg.get("instrument_type") or "OPTION") == "EQUITY":
        size_txt = f"risking {sizing['risk_pct']}% of capital" if sizing.get("mode") == "RISK_PCT" else f"{lots} share{'s' if lots != 1 else ''}"
        return f"{action} {size_txt} of the underlying (equity)"
    size_txt = f"risking {sizing['risk_pct']}% of capital" if sizing.get("mode") == "RISK_PCT" else f"{lots} {'lot' if lots == 1 else 'lots'}"
    phrase = f"{action} {size_txt} {_strike_phrase(leg)} {leg.get('option_type')}"
    if leg.get("expiry_mode"):
        phrase += f" ({leg['expiry_mode'].lower()} expiry)"
    return phrase


def describe_rules(rules: dict | None, symbol: str = "") -> str:
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

    expiry_rule = rules.get("expiry") or {}
    expiry_mode = expiry_rule.get("mode", "WEEKLY")
    expiry_offset = expiry_rule.get("expiry_offset") or 0
    cycle_noun = "week" if expiry_mode == "WEEKLY" else "month"
    offset_txt = f", {expiry_offset} {cycle_noun}{'s' if expiry_offset != 1 else ''} out" if expiry_offset else ""
    sentence += f" ({expiry_mode.lower()} expiry default{offset_txt})"
    if rules.get("product") == "INTRADAY":
        sentence += " [INTRADAY — MIS, forced square-off same day, never held overnight]"

    entry = rules.get("entry") or {}
    if entry.get("mode") == "AT_TIME" and entry.get("time"):
        sentence += f", enter at {entry['time']}"
        if entry.get("weekday"):
            sentence += f" on {entry['weekday'].title()}"
    elif entry.get("mode") == "CONDITIONAL" and entry.get("condition"):
        c = entry["condition"]
        if c.get("type") == "MA_CROSSOVER":
            sentence += f", enter when price crosses {c.get('direction', '?').lower()} its {c.get('period_days')}-day average"
        elif c.get("type") == "IV_RANK":
            sentence += f", enter when IV rank is {c.get('operator', '?').lower()} {c.get('threshold')}"
    elif entry.get("mode") == "BEFORE_EXPIRY" and entry.get("before_expiry"):
        b = entry["before_expiry"]
        sentence += f", enter within {b.get('days_before_expiry')} day(s) of expiry"
        if b.get("weekday"):
            sentence += f" (preferring {b['weekday'].title()})"
        if b.get("time"):
            sentence += f" at {b['time']}"
    else:
        sentence += ", enter immediately when the strategy goes live"

    exit_ = rules.get("exit") or {}
    exit_bits = []
    if exit_.get("take_profit_pct"):
        exit_bits.append(f"+{exit_['take_profit_pct']}% profit")
    if exit_.get("take_profit_amount"):
        exit_bits.append(f"+₹{exit_['take_profit_amount']:,.0f} profit")
    if exit_.get("take_profit_capital_pct"):
        exit_bits.append(f"+{exit_['take_profit_capital_pct']}% of deployed capital")
    if exit_.get("stop_loss_capital_pct"):
        exit_bits.append(f"-{exit_['stop_loss_capital_pct']}% of deployed capital")
    if (exit_.get("stop_loss_mode") or "PCT") == "BREAKEVEN":
        exit_bits.append("spot crossing the position's breakeven")
    elif exit_.get("stop_loss_pct"):
        exit_bits.append(f"-{exit_['stop_loss_pct']}% loss")
    if exit_.get("exit_time"):
        weekday_txt = f" {exit_['exit_weekday'].title()}" if exit_.get("exit_weekday") else ""
        exit_bits.append(f"{exit_['exit_time']}{weekday_txt} time exit")
    if exit_.get("exit_days_before_expiry"):
        d = exit_["exit_days_before_expiry"]
        exit_bits.append(f"{d} day{'s' if d != 1 else ''} before expiry")
    sentence += ", exit on " + (" or ".join(exit_bits) if exit_bits else "expiry only")
    if any(leg.get("exit") for leg in rules["legs"]):
        sentence += " (some legs have their own independent exit rules)"
    return sentence + "."
