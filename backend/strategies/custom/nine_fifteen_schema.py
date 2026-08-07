"""
strategies/custom/nine_fifteen_schema.py — the rule contract for the
"9:15 Opening Range Breakout" strategy (Stockan's "I've Been Using This
9:15 AM Options Buying Strategy for 26 Years!"). See
strategies/custom/nine_fifteen_strategy.py for the executor and
api/nine_fifteen_engine.py for the scheduler that drives it.

Deliberately its OWN schema/executor/scheduler — same reasoning as
intraday_schema.py/zero_to_hero_schema.py's own docstrings. What makes
this one unique even among the other signal-driven engines: WHICH stock
to trade is decided fresh every single day by a live market-open scan
(today's #1 F&O stock gainer and #1 F&O stock loser by real price move),
not a fixed symbol chosen at build time — every other engine in this app,
including Zero to Hero, still trades one pre-selected symbol.
CustomStrategy.symbols is required non-null, so a strategy row using this
schema stores the placeholder ["AUTO"] there and the engine ignores it
entirely, always re-scanning live instead.

A CustomStrategy row using this schema is marked by
`strategy_type == "NINE_FIFTEEN_ORB"` (see db/models.py) — that value is
what routes custom_strategy_scheduler.py to SKIP it and
api/nine_fifteen_engine.py to pick it up instead (dispatched via
api/strategy_scheduler.py's shared tick loop).

Rules JSON shape::

    {
      "lots": <int> >= 1,                # default 1 — applies independently to BOTH the
                                         # gainer-CE and loser-PE legs (they're two
                                         # unrelated single-lot decisions, not a combined size)
      "scan_time": "HH:MM",              # default "09:15" — when to run the live top-gainer/
                                         # top-loser scan across every F&O stock
      "observation_seconds": <int> >= 10, # default 45 — the video's own "watch the first 30-60
                                         # seconds before acting" rule; no entry is even
                                         # considered before this many seconds past scan_time
      "entry_cutoff_time": "HH:MM",      # default "09:20" — stop waiting for a breakout after
                                         # this; if the opening-price breach never happened,
                                         # that side is skipped for the day (never chased late)
      "exit_time": "HH:MM",              # default "09:30" — hard square-off, "Say Good Night to
                                         # the market" — no target/stop-loss otherwise; the
                                         # video's own risk control IS the short holding period
      "min_pct_move": <number> >= 0,     # default 0 — skip a side entirely if today's #1
                                         # mover hasn't moved at least this % from its previous
                                         # close (0 = always trade whoever's #1, no floor)
    }
"""

_DEFAULTS = {
    "scan_time": "09:15",
    "observation_seconds": 45,
    "entry_cutoff_time": "09:20",
    "exit_time": "09:30",
    "min_pct_move": 0,
}


def _is_hhmm(value) -> bool:
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        return False
    h, m = value[:2], value[3:]
    return h.isdigit() and m.isdigit() and 0 <= int(h) <= 23 and 0 <= int(m) <= 59


def get_setting(rules: dict, key: str):
    """Every field except `lots` has a documented default — this is the one place that default is applied, so the executor/scheduler/description never have to repeat it."""
    return rules.get(key, _DEFAULTS.get(key))


def validate_nine_fifteen_rules(rules: dict) -> list[str]:
    """Return a list of human-readable error strings; empty list = valid. Mirrors intraday_schema.validate_intraday_rules()'s contract exactly, just for this shape."""
    errors: list[str] = []
    if not isinstance(rules, dict):
        return ["Strategy rules must be an object."]

    lots = rules.get("lots", 1)
    if not isinstance(lots, int) or lots < 1:
        errors.append("'lots' must be a positive whole number.")

    for key in ("scan_time", "entry_cutoff_time", "exit_time"):
        value = get_setting(rules, key)
        if not _is_hhmm(value):
            errors.append(f"'{key}' must be in HH:MM format.")

    observation_seconds = get_setting(rules, "observation_seconds")
    if not isinstance(observation_seconds, int) or observation_seconds < 10:
        errors.append("'observation_seconds' must be a whole number of 10 or more.")

    min_pct_move = get_setting(rules, "min_pct_move")
    if not isinstance(min_pct_move, (int, float)) or min_pct_move < 0:
        errors.append("'min_pct_move' must be a non-negative number.")

    scan_time, cutoff, exit_time = get_setting(rules, "scan_time"), get_setting(rules, "entry_cutoff_time"), get_setting(rules, "exit_time")
    if _is_hhmm(scan_time) and _is_hhmm(cutoff) and scan_time >= cutoff:
        errors.append("'entry_cutoff_time' must be after 'scan_time'.")
    if _is_hhmm(cutoff) and _is_hhmm(exit_time) and cutoff >= exit_time:
        errors.append("'exit_time' must be after 'entry_cutoff_time'.")

    return errors


def describe_nine_fifteen_rules(rules: dict | None, symbol: str = "") -> str:
    """Plain-English summary — same role as intraday_schema.describe_intraday_rules() for this shape. `symbol` is unused (see this module's docstring — the traded symbol is never fixed)."""
    if not rules:
        return "No rules configured yet."
    lots = rules.get("lots", 1)
    scan_time = get_setting(rules, "scan_time")
    cutoff = get_setting(rules, "entry_cutoff_time")
    exit_time = get_setting(rules, "exit_time")
    observation_seconds = get_setting(rules, "observation_seconds")
    min_pct_move = get_setting(rules, "min_pct_move")
    floor = f", only if it's moved at least {min_pct_move}%" if min_pct_move else ""
    return (
        f"At {scan_time}, scan every F&O stock and find today's #1 gainer and #1 loser by live price move{floor}. "
        f"After a {observation_seconds}s observation window, BUY {lots} lot{'s' if lots != 1 else ''} of the ATM CALL "
        f"on the gainer once it breaks back above its own opening price, and {lots} lot{'s' if lots != 1 else ''} of "
        f"the ATM PUT on the loser once it breaks back below its opening price — each independently, up until "
        f"{cutoff} (skipped for the day if it never breaks). Square off everything by {exit_time}, no exceptions."
    )
