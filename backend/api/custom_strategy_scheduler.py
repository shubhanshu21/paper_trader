"""
api/custom_strategy_scheduler.py — the LEG-BASED engine for user-built
custom strategies (CustomStrategy rows with strategy_type CUSTOM or a
legacy value like STRADDLE/STRANGLE/IRON_CONDOR/BUTTERFLY): entry/exit
logic (_try_entry/_try_exit/_tick_one_strategy) plus the type-agnostic
live-position reconciliation and periodic P&L push
(_reconcile_live_positions/_send_pnl_updates, used for EVERY strategy
type, not just this engine's own).

This module no longer owns a background task/event loop itself — see
api/strategy_scheduler.py, the single shared scheduler that ticks every
CustomStrategy row (any strategy_type) and dispatches to the right
engine's _tick_one_strategy. This module was one of three separate
per-engine loops until they were unified there; only the loop was
unified, none of the entry/exit logic below changed.

This is deliberately independent of run_daemon.py/run_strategy.py (the
.env/STRATEGY_CONFIGS-driven system for hand-written strategies like
TenPercentOTMStrangle) — those are keyed by a static strategy name, not a
DB row a user can create/edit/delete at runtime. Custom strategies get
their own lightweight engine here instead of being forced through that
static config path.

Each tick (called from strategy_scheduler.py, only during real NSE market hours):
  1. ENTRY — for every PAPER_TRADING/LIVE strategy (PAUSED strategies are
     skipped — pausing means "don't take new entries") with no OPEN
     basket and no basket already entered for the CURRENT expiry cycle,
     run its RuleBasedStrategy once and record the resulting legs as OPEN
     CustomStrategyPosition rows. "Current cycle" is tracked per symbol,
     not per calendar day — a strategy that exits early (TP/SL hit well
     before expiry) must NOT re-enter a fresh basket on the same
     soon-to-expire contract the next morning; it waits until the
     resolved nearest expiry actually rolls over to the next one (see
     _resolve_current_expiry/_get_last_entered_expiry). This is what
     makes "enter once, hold through the cycle, don't come back until
     the next cycle starts" the real behavior, for every entry mode.
  2. EXIT — for every PAPER_TRADING/LIVE/PAUSED strategy with an OPEN
     basket, price it and check take-profit / stop-loss / exit-time /
     exit-days-before-expiry; square off every leg and mark the basket
     CLOSED if any trigger fires. PAUSED strategies are included here
     (only entry is skipped) — otherwise pausing a strategy would abandon
     any already-open real position with zero further management until
     it's resumed or stopped, which is exactly the gap that used to exist
     for STOPPED too (see routes_custom_strategies.py::update_strategy_status,
     which now square-offs on STOP; PAUSE deliberately does NOT square
     off — it's meant to be reversible — so exit management must instead
     keep running for whatever's already open).
"""
import json
import threading
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import text, update

from compliance.sebi_rules import (
    AuditTrail,
    OrderRateLimiter,
    assert_market_is_open,
    get_global_kill_switch,
)
from db.engine import SessionLocal
from db.models import CustomStrategy, CustomStrategyPosition
from strategies.custom.rule_strategy import RuleBasedStrategy
from utils.instrument_cache import InstrumentCache
from utils.logger import get_logger
from utils.notify import notify
from utils.option_utils import check_exit_trigger, is_within_pre_expiry_buffer
from utils.telegram_alert import alert_pnl_update, alert_trade_closed, alert_trade_opened
from utils.trailing_stop import advance_trailing_stop, stop_triggered

log = get_logger(__name__)

# _TICK_SEC_OPEN/_TICK_SEC_CLOSED and the reconciliation/P&L-update
# interval constants (_RECONCILE_INTERVAL_SEC/_PNL_UPDATE_INTERVAL_SEC)
# now live in api/strategy_scheduler.py, the single shared loop that
# calls _tick_one_strategy/_reconcile_live_positions/_send_pnl_updates
# below — this module no longer owns its own tick cadence.

# entry/exit "time" rules (e.g. "10:00") are entered by the user as NSE
# market-hours-of-day (IST) — the server itself runs on UTC, so comparing
# them against a naive datetime.now() would silently compare against the
# wrong clock (see _try_entry/_try_exit below, which is the whole reason
# this exists — sebi_rules.assert_market_is_open already gets this right).
_IST = ZoneInfo("Asia/Kolkata")

_audit = AuditTrail(audit_log_path="logs/custom_strategy_audit.log")
_kill_switch = get_global_kill_switch()
_rate_limiter = OrderRateLimiter(max_per_second=10)
_brokers: dict | None = None
# Lock protecting _brokers — the asyncio scheduler loop reads it from
# the event-loop thread, while reset_brokers_cache() (called from the
# token_refresh_scheduler, which runs in a ThreadPoolExecutor thread)
# writes it.  A plain threading.Lock is sufficient: asyncio tasks run
# single-threaded, so within the event loop this is effectively
# uncontested; the lock only matters for the inter-thread writes.
_brokers_lock = threading.Lock()
# Module-level singleton (not constructed fresh per call) so its in-memory
# instrument-master DataFrame is loaded once and reused — see
# _is_leg_for_symbol below, which looks up real instrument_key -> symbol
# mappings from it on every tick.
_instrument_cache = InstrumentCache()


def _market_is_open_now() -> bool:
    try:
        assert_market_is_open()
        return True
    except RuntimeError:
        return False


def _get_brokers() -> dict | None:
    """Lazily build the paper/live broker pair; retried every tick until it succeeds (e.g. token not ready yet)."""
    global _brokers
    with _brokers_lock:
        if _brokers is not None:
            return _brokers
        try:
            from broker.broker_factory import BrokerFactory
            _brokers = BrokerFactory.create_mode_brokers()
        except Exception as exc:
            log.warning("custom_strategy_scheduler: broker init not ready yet (%s) — will retry.", exc)
            return None
        return _brokers


def reset_brokers_cache() -> None:
    """
    Drop the cached paper/live broker pair so the next _get_brokers() call
    rebuilds them from scratch — in particular so UpstoxBroker's underlying
    SDK client picks up a freshly-refreshed access_token. Without this, a
    successful ensure_fresh_upstox_token() only updates the DB-stored
    token; every UpstoxBroker instance built before that point keeps using
    the (now stale) token baked into its SDK Configuration object forever,
    since nothing else ever mutates it in place. Called from
    auth/upstox_auto_login.py right after a successful refresh — see
    _invalidate_broker_caches() there.
    """
    global _brokers
    with _brokers_lock:
        _brokers = None


def _mode_for_status(status: str) -> str:
    return "paper" if status == "PAPER_TRADING" else "live"


def _resolve_product(rules: dict) -> str:
    """rules.product (rule_schema.py: "DELIVERY" | "INTRADAY", defaults to DELIVERY) -> the broker product code. Used at BOTH entry (RuleBasedStrategy construction) and close (_close_leg) — an MIS-entered leg MUST be closed with product="MIS" too, never the NRML default, or the broker has no matching intraday position to net the closing order against."""
    return "MIS" if rules.get("product") == "INTRADAY" else "NRML"


import re
from collections import defaultdict

_SYMBOL_PREFIX_RE_CACHE: dict = {}


def _is_leg_for_symbol(instrument_key: str, symbol: str) -> bool:
    """
    Does this leg's instrument_key belong to `symbol`'s underlying?

    A real Upstox instrument_key is an OPAQUE numeric token (e.g.
    'NSE_FO|144247') — the symbol is NOT embedded in it at all, so no
    amount of string-parsing the key itself can ever recover it (this was
    the bug: the old version tried exactly that, and always returned
    False for real broker keys — silently breaking exit-trigger grouping,
    Greeks, and the positions/leaderboard endpoints for every real paper/
    live position). The real symbol has to be looked up from the
    instrument master by instrument_key, where it's stored in encoded
    form (e.g. 'RELIANCE26AUG1430CE').

    backtest/custom_engine.py's MockBroker uses a separate synthetic key
    format instead ('BHAV|SYMBOL|expiry|strike|type', see
    bhavcopy_data_feed.py) that also isn't in the instrument master —
    handled as its own case below.
    """
    if not instrument_key or "|" not in instrument_key:
        return False

    prefix, rest = instrument_key.split("|", 1)
    if prefix == "BHAV":
        return rest.split("|", 1)[0] == symbol

    try:
        df = _instrument_cache.get_or_refresh()
        matches = df.loc[df["instrument_key"] == instrument_key, "symbol"]
    except Exception:
        log.debug("_is_leg_for_symbol: instrument master lookup failed for %s", instrument_key)
        return False
    if matches.empty:
        return False
    encoded = str(matches.iloc[0])
    if encoded == symbol:
        return True
    pattern = _SYMBOL_PREFIX_RE_CACHE.get(symbol)
    if pattern is None:
        pattern = re.compile(rf"^{re.escape(symbol)}\d{{2}}")
        _SYMBOL_PREFIX_RE_CACHE[symbol] = pattern
    return bool(pattern.match(encoded))


def _leg_groups(rules: dict) -> dict:
    """
    Group leg indices by their EFFECTIVE expiry_mode (a leg's own
    rule_schema.py `expiry_mode` override, or the strategy's default
    `expiry.mode`) — legs sharing a mode enter/exit their basket TOGETHER
    as one unit (today's only behavior: every leg shares the strategy
    default, so this returns exactly one group containing every leg
    index). A leg with a DIFFERENT mode from the rest (a calendar spread
    — e.g. a near-week short + a far-week long at the same strike) gets
    its own independent group, re-entered/rolled on its own cycle instead
    of being forced to wait for the other group's expiry to roll too.
    Returns {mode: [leg_index, ...]}.
    """
    default_mode = (rules.get("expiry") or {}).get("mode", "WEEKLY")
    groups: dict = defaultdict(list)
    for i, leg in enumerate(rules["legs"]):
        if (leg.get("instrument_type") or "OPTION") == "EQUITY":
            # No expiry concept at all — always its own group (never
            # inherits the strategy's WEEKLY/MONTHLY default), gated by
            # calendar day instead of a real listed expiry — see
            # _resolve_current_expiry's mode="EQUITY" case.
            groups["EQUITY"].append(i)
        else:
            groups[leg.get("expiry_mode") or default_mode].append(i)
    return dict(groups)


def _get_last_entered_expiry(strategy: CustomStrategy, symbol: str, mode: str) -> str | None:
    """
    The expiry date this symbol's `mode`-cycle basket (see _leg_groups)
    was last entered for, or None. Stored in the same last_entry_date
    JSON column (name kept for backward compatibility — no migration
    needed); the shape is now {symbol: {mode: {"expiry": ..., "date": ...}}}
    — nested one level deeper than before, by expiry mode, so a calendar
    spread's two (or more) independently-cycling leg groups don't
    clobber each other's cycle-tracking under the same symbol key. A
    pre-existing row in the OLD flat {symbol: {"expiry": ...}} shape (or
    an even older bare date string) is treated as "no expiry recorded
    yet for this mode" — worst case one extra entry attempt gets made
    once, which correctly checks has_open_position first anyway; this is
    the exact same graceful-degradation the old flat shape already
    documented for ITS predecessor.
    """
    if not strategy.last_entry_date:
        return None
    try:
        data = json.loads(strategy.last_entry_date)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    symbol_entry = data.get(symbol)
    if not isinstance(symbol_entry, dict):
        return None
    mode_entry = symbol_entry.get(mode)
    return mode_entry.get("expiry") if isinstance(mode_entry, dict) else None


def _get_last_entered_margin(strategy: CustomStrategy, symbol: str, mode: str) -> float | None:
    """
    The REAL broker-reported margin (₹) blocked for this symbol's `mode`-
    cycle basket at entry (see _try_entry's get_basket_required_margin
    call), or None if never recorded — either an old row from before this
    existed, or the broker couldn't report basket margin that cycle (e.g.
    MockBroker/backtest). Same JSON blob as _get_last_entered_expiry,
    just one more key on the same mode_entry dict — no migration needed.
    Used by exit.take_profit_capital_pct/stop_loss_capital_pct (see
    rule_schema.py) — those checks simply never fire if this returns
    None, rather than guessing a capital base.
    """
    if not strategy.last_entry_date:
        return None
    try:
        data = json.loads(strategy.last_entry_date)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    symbol_entry = data.get(symbol)
    if not isinstance(symbol_entry, dict):
        return None
    mode_entry = symbol_entry.get(mode)
    margin = mode_entry.get("margin") if isinstance(mode_entry, dict) else None
    return float(margin) if isinstance(margin, (int, float)) else None


def _set_last_entered_expiry(strategy: CustomStrategy, symbol: str, mode: str, expiry: str, margin_at_entry: float | None = None) -> None:
    try:
        data = json.loads(strategy.last_entry_date) if strategy.last_entry_date else {}
        if not isinstance(data, dict):
            data = {}
    except json.JSONDecodeError:
        data = {}
    symbol_entry = data.get(symbol)
    if not isinstance(symbol_entry, dict):
        symbol_entry = {}
    symbol_entry[mode] = {"expiry": expiry, "date": date.today().isoformat(), "margin": margin_at_entry}
    data[symbol] = symbol_entry
    strategy.last_entry_date = json.dumps(data)


def _resolve_current_expiry(broker, symbol: str, mode: str, offset: int = 0) -> str | None:
    """
    Lightweight pre-flight expiry resolution for ONE expiry mode — used
    ONLY to decide whether this symbol's `mode`-cycle has already been
    traded, before doing the full (order-placing) RuleBasedStrategy run.
    Deliberately duplicates the couple of calls
    RuleBasedStrategy._resolve_expiries_and_chains() also makes
    internally, rather than sharing code, since this must run BEFORE any
    order can be placed (can't ask "did we already trade this cycle" by
    first running the very thing that would trade it).

    mode="EQUITY" (see _leg_groups) has no real expiry to resolve at
    all — reuses this same "already traded this cycle?" gate, keyed by
    TODAY'S DATE instead of a listed expiry, so an equity leg group
    naturally re-enters at most once per calendar day rather than every
    tick, without inventing a second gating mechanism.
    """
    if mode == "EQUITY":
        return date.today().isoformat()

    from utils.option_utils import find_expiry_by_type_offset, find_nearest_expiry_by_type

    try:
        instrument_key = broker.resolve_instrument_key(symbol)
        expiries = broker.get_option_contracts(instrument_key)
        if not expiries:
            return None
        now = broker.get_current_time()
        reference_date = now.date() if now else None
        if offset:
            return find_expiry_by_type_offset(expiries, mode, offset, reference_date=reference_date)
        return find_nearest_expiry_by_type(expiries, mode, reference_date=reference_date)
    except Exception as exc:
        log.warning("custom_strategy_scheduler: could not pre-resolve %s expiry for %s: %s", mode, symbol, exc)
        return None


def _ma_crossover_met(broker, symbol: str, instrument_type: str, condition: dict) -> bool:
    """
    entry.condition {"type": "MA_CROSSOVER", "period_days", "direction"}
    — reuses the EXISTING fno_bhavcopy historical daily-close data (the
    same table backtest/custom_engine.py already reads — no new data
    pipeline needed) for the trailing N closes, plus a live LTP for
    "today," rather than a real intraday moving average. Returns False
    (never triggers) if there isn't yet `period_days` of history, or on
    any lookup failure — an entry condition that can't be evaluated
    safely defaults to "not met," never a guess.
    """
    period = condition.get("period_days")
    direction = condition.get("direction")
    if not isinstance(period, int) or period < 2 or direction not in ("ABOVE", "BELOW"):
        return False

    future_instrument = "FUTIDX" if instrument_type == "INDEX" else "FUTSTK"
    db = SessionLocal()
    try:
        rows = db.execute(
            text(
                "SELECT close FROM fno_bhavcopy WHERE symbol=:symbol AND instrument=:instrument "
                "AND expiry_dt >= trade_date ORDER BY trade_date DESC LIMIT :n"
            ),
            {"symbol": symbol, "instrument": future_instrument, "n": period},
        ).fetchall()
    except Exception as exc:
        log.warning("custom_strategy_scheduler: MA crossover history lookup failed for %s: %s", symbol, exc)
        return False
    finally:
        db.close()

    closes = [float(r[0]) for r in rows if r[0] is not None]
    if len(closes) < period:
        return False  # not enough history yet — safe default, no entry

    moving_average = sum(closes) / len(closes)
    try:
        ltp = broker.get_ltp(broker.resolve_instrument_key(symbol))
    except Exception:
        ltp = None
    if ltp is None:
        return False

    return ltp > moving_average if direction == "ABOVE" else ltp < moving_average


def _iv_rank_condition_met(symbol: str, condition: dict) -> bool:
    """entry.condition {"type": "IV_RANK", "operator", "threshold"} — see utils/iv_rank.py. None (insufficient history) always means "not met," never a fabricated trigger."""
    operator = condition.get("operator")
    threshold = condition.get("threshold")
    if operator not in ("ABOVE", "BELOW") or not isinstance(threshold, (int, float)):
        return False

    from utils.iv_rank import compute_iv_rank
    rank = compute_iv_rank(symbol)
    if rank is None:
        return False
    return rank > threshold if operator == "ABOVE" else rank < threshold


def _entry_condition_met(broker, symbol: str, condition: dict, instrument_type: str) -> bool:
    """CONDITIONAL entry gate (see rule_schema.py's entry.condition). Never raises — any evaluation failure is treated as "not met," a safe default over guessing."""
    condition_type = condition.get("type")
    try:
        if condition_type == "MA_CROSSOVER":
            return _ma_crossover_met(broker, symbol, instrument_type, condition)
        if condition_type == "IV_RANK":
            return _iv_rank_condition_met(symbol, condition)
    except Exception as exc:
        log.warning("custom_strategy_scheduler: entry condition check failed for %s (%s): %s", symbol, condition_type, exc)
    return False


_WEEKDAY_NUMS = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}
# Unconditional hard exit for rules.product == "INTRADAY" (rule_schema.py) —
# a few minutes ahead of NSE's 15:30 close, same buffer-before-close
# convention as the other intraday-only engines in this app (e.g. Supertrend's
# own exit_time default). Fires regardless of exit_time/take_profit/
# stop_loss state — this product choice NEVER holds a position overnight.
INTRADAY_SQUAREOFF_TIME = "15:20"
# If the preferred weekday never lands inside the window (a holiday, or a
# short month), force entry once we're down to this many days before
# expiry rather than silently skip the WHOLE monthly cycle waiting for a
# weekday that isn't coming — same "never miss a cycle" discipline
# _resolve_current_expiry's caller already relies on elsewhere.
_BEFORE_EXPIRY_LATEST_DAYS_FALLBACK = 1


def _before_expiry_eligible(before_rule: dict, expiry_str: str) -> bool:
    """entry.mode == "BEFORE_EXPIRY" (see rule_schema.py) — is TODAY inside this group's entry window for `expiry_str`?"""
    expiry_date = dtime_or_none(expiry_str)
    if expiry_date is None:
        return False
    today = date.today()
    days_before = before_rule.get("days_before_expiry")
    if not isinstance(days_before, int) or days_before < 1:
        return False
    if not is_within_pre_expiry_buffer(today, expiry_date, days_before):
        return False  # window not open yet

    time_gate = before_rule.get("time")
    if time_gate and datetime.now(_IST).strftime("%H:%M") < time_gate:
        return False

    weekday = before_rule.get("weekday")
    if weekday and _WEEKDAY_NUMS.get(weekday) != today.weekday():
        # Not the preferred weekday — allow it anyway only once we've hit
        # the last-chance fallback window, so a holiday on the target
        # weekday can't skip the whole cycle.
        if not is_within_pre_expiry_buffer(today, expiry_date, _BEFORE_EXPIRY_LATEST_DAYS_FALLBACK):
            return False
    return True


def _try_entry(db, strategy: CustomStrategy, broker) -> None:
    symbols = json.loads(strategy.symbols)
    rules = json.loads(strategy.rules_json)
    entry_rule = rules.get("entry") or {}
    entry_mode = entry_rule.get("mode")

    if entry_mode == "AT_TIME":
        target = entry_rule.get("time")
        now = datetime.now(_IST)
        if now.strftime("%H:%M") < target:
            return
        entry_weekday = entry_rule.get("weekday")
        if entry_weekday and _WEEKDAY_NUMS.get(entry_weekday) != now.weekday():
            return

    # Fetch all open positions for this strategy
    open_positions = db.query(CustomStrategyPosition).filter(
        CustomStrategyPosition.strategy_id == strategy.id,
        CustomStrategyPosition.status == "OPEN",
    ).all()

    # {expiry_mode: [leg_index, ...]} — one group for a normal (single-
    # expiry) strategy, 2+ for a calendar spread. See _leg_groups().
    groups = _leg_groups(rules)

    # expiry_offset only applies to the strategy's own default expiry mode
    # — a calendar-spread override group always resolves its nearest (0).
    # Mirrors rule_strategy.py::_resolve_expiries_and_chains exactly, so
    # this pre-flight "already traded this cycle?" check and the real
    # order-placing run always agree on which expiry is "current".
    default_expiry_rule = rules.get("expiry") or {}
    default_mode = default_expiry_rule.get("mode", "WEEKLY")
    default_offset = default_expiry_rule.get("expiry_offset") or 0

    for symbol in symbols:
        # CONDITIONAL entry is per-SYMBOL (an MA crossover or IV rank is
        # computed against one underlying at a time), unlike AT_TIME
        # above which is strategy-wide — so this check lives inside the
        # per-symbol loop instead of gating the whole function.
        if entry_mode == "CONDITIONAL" and not _entry_condition_met(broker, symbol, entry_rule.get("condition") or {}, strategy.instrument_type):
            continue

        for mode, leg_indices in groups.items():
            # Does THIS group already hold an open position for this
            # symbol? Only that group's own legs count — a calendar
            # spread's near-week group can need re-entry while its
            # far-week group is still legitimately open.
            group_has_open = any(
                _is_leg_for_symbol(p.instrument_key, symbol) and p.leg_index in leg_indices
                for p in open_positions
            )
            if group_has_open:
                continue

            # Gate on the CURRENT expiry cycle, not the calendar day — an
            # early exit (TP/SL hit) must not trigger a same-cycle
            # re-entry on the same soon-to-expire contract; wait for the
            # resolved nearest expiry to actually roll over to the next one.
            offset = default_offset if mode == default_mode else 0
            current_expiry = _resolve_current_expiry(broker, symbol, mode, offset)
            if current_expiry is None:
                continue  # Couldn't resolve (broker hiccup) — retry next tick, don't mark anything.
            if _get_last_entered_expiry(strategy, symbol, mode) == current_expiry:
                continue  # Already traded this cycle.

            # BEFORE_EXPIRY is expiry-relative (unlike AT_TIME's strategy-
            # wide gate above), so it can only be checked once this
            # group's real resolved expiry is known — "enter close to
            # THIS expiry", not right after the PREVIOUS one rolled over.
            if entry_mode == "BEFORE_EXPIRY" and not _before_expiry_eligible(entry_rule.get("before_expiry") or {}, current_expiry):
                continue

            try:
                rule_strategy = RuleBasedStrategy(
                    broker=broker, audit=_audit, kill_switch=_kill_switch, rate_limiter=_rate_limiter,
                    symbol=symbol, rules=rules, product=_resolve_product(rules), user_id=strategy.user_id,
                )
                result = rule_strategy.run(leg_indices=leg_indices)
            except Exception as exc:
                log.error("custom_strategy_scheduler: entry failed for strategy %s (%s) symbol %s [%s cycle]: %s",
                          strategy.id, strategy.name, symbol, mode, exc)
                notify(
                    "custom_strategy",
                    f"Entry failed for strategy \"{strategy.name}\" ({symbol}, {_mode_for_status(strategy.status)} mode): {exc}. "
                    f"Will keep retrying every tick until the current expiry cycle rolls over.",
                    level="warning",
                    user_id=strategy.user_id,
                )
                # No _set_last_entered_expiry() here — nothing was placed, so
                # retry next tick rather than silently giving up on this cycle.
                continue

            if result.get("status") not in ("success", "dry_run"):
                continue
            entered_expiry = (result.get("expiries") or {}).get(mode) or current_expiry

            # Snapshot the REAL broker-reported basket margin ONCE, right
            # at entry — the only correct denominator for exit.
            # take_profit_capital_pct/stop_loss_capital_pct (rule_schema.py):
            # "% of deployed capital" means % of what the broker actually
            # blocked for this multi-leg basket, not % of premium
            # collected. Never guessed: None (unsupported broker, e.g.
            # MockBroker/backtest, or a lookup failure) means those two
            # exit checks simply never fire for this cycle — see
            # _get_last_entered_margin/_try_exit.
            margin_at_entry = None
            option_instruments = [
                {"instrument_key": leg["instrument_token"], "quantity": leg["quantity"],
                 "transaction_type": leg["transaction_type"], "product": rule_strategy.product}
                for leg in result["legs"] if leg["instrument_type"] == "OPTION"
            ]
            if option_instruments:
                try:
                    margin_at_entry = broker.get_basket_required_margin(option_instruments)
                except Exception as exc:
                    log.warning("custom_strategy_scheduler: basket margin lookup failed for strategy %s (%s) symbol %s: %s",
                                strategy.id, strategy.name, symbol, exc)

            _set_last_entered_expiry(strategy, symbol, mode, entered_expiry, margin_at_entry)

            db_mode = _mode_for_status(strategy.status)
            for original_idx, leg in zip(result["leg_indices"], result["legs"], strict=False):
                leg_exit_config = rules["legs"][original_idx].get("exit")
                trail_state = None
                if leg_exit_config and (leg_exit_config.get("trailing") or {}).get("enabled"):
                    trail_state = json.dumps({"highest_price": None, "lowest_price": None, "current_stop_price": None})
                db.add(CustomStrategyPosition(
                    strategy_id=strategy.id, leg_index=original_idx, mode=db_mode,
                    instrument_key=leg["instrument_token"], instrument_type=leg["instrument_type"],
                    option_type=leg["option_type"], strike=leg["strike"], expiry=leg["expiry"],
                    transaction_type=leg["transaction_type"], quantity=leg["quantity"],
                    entry_price=leg["entry_price"] or 0, order_id=leg.get("order_id"), status="OPEN",
                    leg_config_json=json.dumps(leg_exit_config) if leg_exit_config else None,
                    trail_state_json=trail_state,
                ))
            db.commit()
            log.info("custom_strategy_scheduler: entered strategy %s (%s) symbol %s [%s cycle] — %d legs.",
                     strategy.id, strategy.name, symbol, mode, len(result["legs"]))

            leg_details = " | ".join(
                f"{leg['transaction_type']} {leg['option_type']} {leg['strike']}@{leg['entry_price'] or 0:.2f}"
                for leg in result["legs"]
            )
            qty = result["legs"][0]["quantity"] if result["legs"] else None
            details = f"{leg_details} | qty={qty}" if qty is not None else leg_details
            # level="trade" (not "info"/"warning"/"error") — NotificationBell.tsx
            # renders it as its own "trade opened" card (icon + strategy name +
            # per-leg detail rows) instead of a flat info line; notify() itself
            # only pushes to Telegram for "warning"/"error", so the nicely
            # formatted Telegram message below is sent separately.
            notify("custom_strategy", f"Trade Opened — {strategy.name}\n{symbol} ({db_mode})\n{details}",
                   level="trade", user_id=strategy.user_id)
            alert_trade_opened(strategy.name, db_mode, symbol, details)


def _combined_pnl(legs: list[CustomStrategyPosition], now_prices: dict, rates: dict | None = None) -> tuple[float, float] | None:
    """
    (pnl_amount, pnl_pct) — NET P&L (₹, and as % of the combined premium
    at entry) — includes real Upstox transaction costs (brokerage/STT/
    exchange charges/GST/SEBI charges/stamp duty, see utils/costs.py) on
    both the original entry AND the hypothetical exit at `now_prices`,
    same as backtest/custom_engine.py computes for historical cycles.
    Slippage is already baked into leg.entry_price/now_prices themselves
    (PaperBroker/UpstoxBroker apply it at fill time) — this function only
    adds the fee layer backtest already had and paper/live previously
    didn't, so the two modes are now comparable apples-to-apples.

    Used for both TP/SL trigger checks (a "+40% profit" or a flat
    "+₹4,500 profit" target is evaluated net of real costs, not a
    false-positive gross number) and for the final stored
    paper_return_pct/live_return_pct. Returns None if any leg's current
    price is unavailable (never guesses a partial P&L).
    """
    from utils.costs import calculate_leg_transaction_cost_breakdown

    pnl_amount = 0.0
    denom = 0.0
    for leg in legs:
        now = now_prices.get(leg.instrument_key)
        if now is None:
            return None
        entry = float(leg.entry_price)
        qty = leg.quantity
        sign = -1 if leg.transaction_type == "SELL" else 1
        pnl_amount += (entry - now) * qty * (-sign)
        denom += entry * qty

        exit_transaction_type = "BUY" if leg.transaction_type == "SELL" else "SELL"
        entry_costs = calculate_leg_transaction_cost_breakdown(leg.instrument_type, entry, qty, leg.transaction_type, rates)
        exit_costs = calculate_leg_transaction_cost_breakdown(leg.instrument_type, now, qty, exit_transaction_type, rates)
        pnl_amount -= entry_costs.get("total", 0) + exit_costs.get("total", 0)
    pnl_pct = (pnl_amount / denom * 100.0) if denom > 0 else 0.0
    return pnl_amount, pnl_pct


def _combined_pnl_pct(legs: list[CustomStrategyPosition], now_prices: dict, rates: dict | None = None) -> float | None:
    """pnl_pct only — see _combined_pnl. Kept for callers (per-leg exit check) that only ever needed the percentage."""
    result = _combined_pnl(legs, now_prices, rates)
    return result[1] if result else None


def _close_leg(db, strategy: CustomStrategy, broker, leg: CustomStrategyPosition, trigger: str, now_prices: dict) -> bool:
    """
    Square off ONE leg via a MARKET order opposite its entry side. Returns
    True iff closed — on failure, alerts (a real position may still be
    open) and leaves the leg OPEN for the next tick to retry.

    Atomically claims the leg (OPEN -> CLOSING) before placing the broker
    order. This matters because a leg can now be closed from two
    independent callers racing on the same row: the scheduler's own tick
    (runs on the asyncio event loop) and a user-triggered Stop
    (routes_custom_strategies.py::update_strategy_status, a sync route
    that FastAPI runs in a threadpool thread) — without this claim, both
    could read the leg as OPEN and each place a real closing order,
    double-exiting it at the broker. Only the caller whose UPDATE
    actually matches a row (claimed == 1) proceeds; the other backs off
    and returns False, same as any other "couldn't close this tick" case.
    """
    claimed = db.execute(
        update(CustomStrategyPosition)
        .where(CustomStrategyPosition.id == leg.id, CustomStrategyPosition.status == "OPEN")
        .values(status="CLOSING")
    ).rowcount
    db.commit()
    if claimed == 0:
        return False  # another caller already claimed (or closed) this leg this instant

    opposite = broker.place_buy_order if leg.transaction_type == "SELL" else broker.place_sell_order
    try:
        _rate_limiter.acquire()
        # MUST match the product this leg was actually ENTERED with (see
        # _resolve_product) — closing an MIS-entered leg with the NRML
        # default has no matching intraday position for the broker to net
        # the closing order against.
        product = _resolve_product(json.loads(strategy.rules_json)) if strategy.rules_json else "NRML"
        exit_order_id = opposite(
            instrument_token=leg.instrument_key, quantity=leg.quantity, product=product, order_type="MARKET",
            tag=f"CUSTOM_EXIT_{strategy.id}_{leg.leg_index}"[:20],
            user_id=strategy.user_id,
        )
    except Exception as exc:
        log.critical(
            "custom_strategy_scheduler: FAILED to square off leg %s for strategy %s: %s — MANUAL INTERVENTION REQUIRED.",
            leg.instrument_key, strategy.id, exc,
        )
        notify(
            "custom_strategy",
            f"MANUAL INTERVENTION REQUIRED — failed to exit \"{strategy.name}\" leg {leg.instrument_key} "
            f"({trigger} triggered): {exc}. A position may still be open on your real account.",
            user_id=strategy.user_id,
        )
        leg.status = "OPEN"  # release the claim so the next tick (or a retried Stop) picks it back up
        db.commit()
        return False
    leg.status = "CLOSED"
    # Prefer the broker's own reported fill price over the pre-order
    # now_prices snapshot — see RuleBasedStrategy.enter()'s matching
    # comment for why a snapshot isn't necessarily what was actually paid.
    fill_price = broker.get_fill_price(exit_order_id) if exit_order_id else None
    if fill_price is None:
        fill_price = now_prices.get(leg.instrument_key)
    if fill_price is None:
        # now_prices is a pre-order BATCH snapshot that can legitimately
        # miss one instrument (rate limit, transient gap) even though the
        # closing order above just filled successfully against a real,
        # still-listed contract — one more single-instrument LTP call is
        # cheap and often succeeds where the batch call didn't.
        try:
            fill_price = broker.get_ltp(leg.instrument_key)
        except Exception:
            fill_price = None
    leg.exit_price = fill_price
    leg.exit_order_id = exit_order_id
    leg.exit_reason = trigger
    leg.closed_at = datetime.now()
    if fill_price is None:
        # A real order filled at the broker (exit_order_id is set) but no
        # price could be captured anywhere — wallet.py/routes_leaderboard.py
        # both null-guard exit_price so this can't crash, but it silently
        # drops this leg's P&L from realized totals unless flagged here.
        log.warning(
            "custom_strategy_scheduler: leg %s for strategy %s closed (order %s) but no exit price could be "
            "captured — P&L for this leg will be excluded from wallet/leaderboard totals until reconciled.",
            leg.instrument_key, strategy.id, exit_order_id,
        )
        notify(
            "custom_strategy",
            f"\"{strategy.name}\" leg {leg.instrument_key} ({leg.transaction_type} {leg.option_type} {leg.strike}) "
            f"closed ({trigger}) but no exit price could be captured. Please check your broker's order/contract "
            f"note for order {exit_order_id} and record the fill price manually if needed — this leg's P&L is "
            f"excluded from your totals until then.",
            level="warning", user_id=strategy.user_id,
        )
    return True


def _try_exit_individual_leg(db, strategy: CustomStrategy, broker, leg: CustomStrategyPosition, now_prices: dict) -> bool:
    """
    Check (and, if triggered, close) ONE leg that has its OWN exit config
    (rule_schema.py's leg.exit, snapshotted into leg_config_json at entry
    — see _try_entry) — independent of the strategy-level combined check
    in _try_exit below. Returns True iff closed. Always leaves
    leg.trail_state_json updated with the latest ratchet position even
    when not triggered — the caller commits it either way.
    """
    config = json.loads(leg.leg_config_json)
    take_profit_pct = config.get("take_profit_pct")
    stop_loss_pct = config.get("stop_loss_pct")
    trailing = config.get("trailing") or {}

    from utils.wallet import get_charge_rates
    rates = get_charge_rates(strategy.user_id)

    trigger = None
    pnl_pct = _combined_pnl_pct([leg], now_prices, rates)
    if pnl_pct is not None:
        trigger = check_exit_trigger(pnl_pct, take_profit_pct, stop_loss_pct)

    if trigger is None and trailing.get("enabled"):
        ltp = now_prices.get(leg.instrument_key)
        if ltp is not None:
            state = json.loads(leg.trail_state_json) if leg.trail_state_json else {
                "highest_price": None, "lowest_price": None, "current_stop_price": None,
            }
            side = leg.transaction_type  # this leg's own entry side IS "the position being protected" — matches advance_trailing_stop's convention directly, no translation needed
            highest, lowest, stop, _advanced = advance_trailing_stop(
                side, ltp, trailing["trail_amount"], trailing["trail_type"],
                state["highest_price"], state["lowest_price"], state["current_stop_price"],
            )
            state["highest_price"], state["lowest_price"], state["current_stop_price"] = highest, lowest, stop
            leg.trail_state_json = json.dumps(state)
            if stop_triggered(side, ltp, stop):
                trigger = "TRAILING_STOP"

    if trigger is None:
        return False

    return _close_leg(db, strategy, broker, leg, trigger, now_prices)


def _breakeven_breached(broker, symbol: str, legs: list[CustomStrategyPosition]) -> bool:
    """
    stop_loss_mode="BREAKEVEN" (see rule_schema.py) — True once the
    underlying's current spot has moved outside this basket's own
    computed breakeven range (utils/option_utils.py::find_breakevens, an
    exact piecewise-linear payoff root-find over every leg's own strike/
    entry_price/quantity, not a fixed distance). False (never triggers)
    if there's no OPTION leg to compute a breakeven from, or the
    underlying's LTP can't be fetched right now — same "can't evaluate
    safely -> don't guess" default every other exit condition in this
    module uses.
    """
    from utils.option_utils import find_breakevens

    leg_dicts = [
        {
            "strike": float(leg.strike), "option_type": leg.option_type,
            "transaction_type": leg.transaction_type, "entry_price": float(leg.entry_price),
            "quantity": leg.quantity,
        }
        for leg in legs if leg.strike is not None and leg.option_type is not None
    ]
    breakevens = find_breakevens(leg_dicts)
    if not breakevens:
        return False
    try:
        spot = broker.get_ltp(broker.resolve_instrument_key(symbol))
    except Exception:
        spot = None
    if spot is None:
        return False
    return spot < min(breakevens) or spot > max(breakevens)


def _try_exit(db, strategy: CustomStrategy, broker) -> None:
    legs = db.query(CustomStrategyPosition).filter(
        CustomStrategyPosition.strategy_id == strategy.id,
        CustomStrategyPosition.status == "OPEN",
    ).all()
    if not legs:
        return

    from utils.wallet import get_charge_rates
    rates = get_charge_rates(strategy.user_id)

    rules = json.loads(strategy.rules_json)
    exit_rule = rules.get("exit") or {}
    take_profit_pct = exit_rule.get("take_profit_pct")
    take_profit_amount = exit_rule.get("take_profit_amount")
    take_profit_capital_pct = exit_rule.get("take_profit_capital_pct")
    stop_loss_capital_pct = exit_rule.get("stop_loss_capital_pct")
    stop_loss_mode = exit_rule.get("stop_loss_mode") or "PCT"
    stop_loss_pct = exit_rule.get("stop_loss_pct")
    exit_time = exit_rule.get("exit_time")
    exit_weekday = exit_rule.get("exit_weekday")
    exit_days_before_expiry = exit_rule.get("exit_days_before_expiry", 0)
    # Real broker margin at entry — the denominator for take_profit_capital_pct/
    # stop_loss_capital_pct (see rule_schema.py). Only meaningful for the
    # strategy's own default expiry-mode group; a true calendar spread's
    # OTHER group(s) aren't covered by this single lookup (same documented
    # limitation as execute()'s single "expiry" field for calendar spreads).
    default_expiry_mode = (rules.get("expiry") or {}).get("mode", "WEEKLY")

    # Group open legs by symbol
    symbols = json.loads(strategy.symbols)
    legs_by_symbol = defaultdict(list)
    for leg in legs:
        leg_symbol = None
        for s in symbols:
            if _is_leg_for_symbol(leg.instrument_key, s):
                leg_symbol = s
                break
        if leg_symbol:
            legs_by_symbol[leg_symbol].append(leg)
        else:
            legs_by_symbol["UNKNOWN"].append(leg)

    for symbol, symbol_legs in legs_by_symbol.items():
        if symbol == "UNKNOWN":
            continue

        tokens = [leg.instrument_key for leg in symbol_legs]
        now_prices = broker.get_ltp_batch(tokens)

        # 1. Legs with their OWN exit config (per-leg TP/SL/trailing) are
        #    checked/closed independently, BEFORE the strategy-level
        #    combined/time/expiry checks below get a look at whatever's
        #    still open. Legs without one are untouched here — same as
        #    today, managed only by the combined check in step 2.
        still_open = []
        for leg in symbol_legs:
            if leg.leg_config_json and _try_exit_individual_leg(db, strategy, broker, leg, now_prices):
                continue  # closed independently
            still_open.append(leg)
        db.commit()  # persist independent closes + any trail_state_json ratchet advances before the group check below
        if not still_open:
            continue

        # 2. Strategy-level combined TP/SL — only over legs WITHOUT their
        #    own exit config (today's exact behavior when no leg has a
        #    per-leg config: combined_managed == still_open == symbol_legs).
        # take_profit_amount (flat ₹) is checked ALONGSIDE take_profit_pct
        # (either firing exits) — a rupee target doesn't scale with
        # premium the way a % target does, so a strategy can want both/
        # either. stop_loss_mode="BREAKEVEN" REPLACES stop_loss_pct
        # entirely (see rule_schema.py) with a spot-crossing check against
        # this basket's own computed breakeven range.
        trigger = None
        combined_managed = [leg for leg in still_open if not leg.leg_config_json]
        if combined_managed:
            pnl_result = _combined_pnl(combined_managed, now_prices, rates)
            if pnl_result is not None:
                pnl_amount, pnl_pct = pnl_result
                margin_at_entry = (
                    _get_last_entered_margin(strategy, symbol, default_expiry_mode)
                    if (take_profit_capital_pct is not None or stop_loss_capital_pct is not None) else None
                )
                if (take_profit_amount is not None and pnl_amount >= take_profit_amount) or (take_profit_pct is not None and pnl_pct >= take_profit_pct) or (take_profit_capital_pct is not None and margin_at_entry and pnl_amount >= margin_at_entry * take_profit_capital_pct / 100.0):
                    trigger = "TAKE_PROFIT"
                elif stop_loss_capital_pct is not None and margin_at_entry and pnl_amount <= -margin_at_entry * stop_loss_capital_pct / 100.0:
                    trigger = "STOP_LOSS"
                elif stop_loss_mode == "BREAKEVEN":
                    if _breakeven_breached(broker, symbol, combined_managed):
                        trigger = "STOP_LOSS"
                elif stop_loss_pct is not None and pnl_pct <= -abs(stop_loss_pct):
                    trigger = "STOP_LOSS"

        # 3. Strategy-level time/expiry — a calendar "hard stop" that
        #    still applies to EVERY still-open leg of this symbol,
        #    including individually-managed ones that survived step 1
        #    (a leg with its own trailing stop shouldn't stay open past
        #    the strategy's own expiry cutoff).
        hard_stop = None
        now = datetime.now(_IST)
        if rules.get("product") == "INTRADAY" and now.strftime("%H:%M") >= INTRADAY_SQUAREOFF_TIME:
            hard_stop = "INTRADAY_SQUAREOFF"
        if hard_stop is None and exit_time and now.strftime("%H:%M") >= exit_time and (not exit_weekday or _WEEKDAY_NUMS.get(exit_weekday) == now.weekday()):
            hard_stop = "TIME_EXIT"
        # exit_days_before_expiry=0 is a real, documented, DELIBERATE
        # value (rule_schema.py: "exit exactly on expiry day, not
        # before") — a truthy check here (`and exit_days_before_expiry`)
        # would silently skip this ENTIRE safety net for it, since 0 is
        # falsy in Python. `is not None` is the correct guard; the
        # field's own .get(..., 0) default above means it's never
        # actually None, so this always runs and lets
        # is_within_pre_expiry_buffer's own semantics (today >= expiry -
        # N days) decide, exactly as documented.
        if hard_stop is None and exit_days_before_expiry is not None:
            expiries = [dtime_or_none(leg.expiry) for leg in still_open if leg.expiry]
            if expiries and any(is_within_pre_expiry_buffer(date.today(), exp, exit_days_before_expiry) for exp in expiries):
                hard_stop = "EXPIRY"

        if hard_stop is not None:
            legs_to_close, trigger = still_open, hard_stop
        elif trigger is not None:
            legs_to_close = combined_managed
        else:
            continue

        closed_legs = [leg for leg in legs_to_close if _close_leg(db, strategy, broker, leg, trigger, now_prices)]
        combined_closed = [leg for leg in closed_legs if leg in combined_managed]

        # Only record a return_pct when every COMBINED-managed leg
        # actually closed — with one leg left OPEN (a failed exit above),
        # that subset isn't fully realized yet and an aggregate number
        # here would misrepresent a still partially-open position as a
        # completed, priced outcome. Independently-managed legs already
        # booked their own P&L via their own trailing/TP-SL close (see
        # _try_exit_individual_leg) — not folded into this aggregate,
        # same as any other per-leg detail; still fully visible on each
        # leg's own CustomStrategyPosition row.
        if combined_managed and len(combined_closed) == len(combined_managed):
            final_pct = _combined_pnl_pct(combined_managed, now_prices, rates) or 0.0
            if strategy.status == "LIVE":
                strategy.live_return_pct = round(final_pct, 4)
            else:
                strategy.paper_return_pct = round(final_pct, 4)
            log.info(
                "custom_strategy_scheduler: exited strategy %s (%s) symbol %s | trigger=%s | pnl_pct=%.2f",
                strategy.id, strategy.name, symbol, trigger, final_pct,
            )
            leg_details = " | ".join(
                f"{leg.transaction_type} {leg.option_type} {leg.strike}@{leg.exit_price or 0:.2f}"
                for leg in combined_closed
            )
            details = f"reason={trigger} | {leg_details} | pnl={final_pct:+.2f}%"
            leg_mode = combined_closed[0].mode if combined_closed else _mode_for_status(strategy.status)
            notify("custom_strategy", f"Trade Closed — {strategy.name}\n{symbol} ({leg_mode})\n{details}",
                   level="trade", user_id=strategy.user_id)
            alert_trade_closed(strategy.name, leg_mode, symbol, details)
        elif combined_managed:
            log.warning(
                "custom_strategy_scheduler: only %d/%d combined-managed legs closed for strategy %s (%s) symbol %s | trigger=%s — "
                "leaving return_pct unchanged, basket still partially open.",
                len(combined_closed), len(combined_managed), strategy.id, strategy.name, symbol, trigger,
            )
        db.commit()


def square_off_all_open_legs(db, strategy: CustomStrategy, brokers: dict, reason: str = "MANUAL_STOP") -> int:
    """
    Immediately close every OPEN leg for `strategy`, regardless of expiry-
    cycle group — used when a user explicitly stops a strategy (see
    routes_custom_strategies.py::update_strategy_status), so the UI's
    "square off any open position" promise on the Stop dialog is actually
    true. This is needed because once a strategy leaves PAPER_TRADING/LIVE
    it's excluded from this scheduler's main loop query above, so any
    still-open legs would otherwise never be touched again.

    Each leg is closed using ITS OWN entry mode (leg.mode) rather than the
    strategy's current status — a leg entered while LIVE keeps using the
    live broker even if the strategy was PAUSED (and re-labeled) before
    being stopped. Returns the number of legs successfully closed; any
    leg that fails to close (broker error, broker unavailable) is left
    OPEN and alerted on, same failure handling as _close_leg's normal
    scheduler-driven callers.
    """
    legs = db.query(CustomStrategyPosition).filter(
        CustomStrategyPosition.strategy_id == strategy.id,
        CustomStrategyPosition.status == "OPEN",
    ).all()
    if not legs:
        return 0

    legs_by_mode = defaultdict(list)
    for leg in legs:
        legs_by_mode[leg.mode].append(leg)

    closed_count = 0
    for mode, mode_legs in legs_by_mode.items():
        broker = brokers.get(mode)
        if broker is None:
            log.error("square_off_all_open_legs: no %s broker available — cannot close %d leg(s) for strategy %s.",
                       mode, len(mode_legs), strategy.id)
            notify(
                "custom_strategy",
                f"Could not square off {len(mode_legs)} open {mode} leg(s) for \"{strategy.name}\" while stopping "
                f"it — broker unavailable. These positions are still OPEN; please close them manually and retry.",
                level="error", user_id=strategy.user_id,
            )
            continue
        tokens = [leg.instrument_key for leg in mode_legs]
        now_prices = broker.get_ltp_batch(tokens)
        for leg in mode_legs:
            if _close_leg(db, strategy, broker, leg, reason, now_prices):
                closed_count += 1
    db.commit()
    return closed_count


def dtime_or_none(expiry_str: str | None):
    if not expiry_str:
        return None
    try:
        return date.fromisoformat(expiry_str)
    except ValueError:
        return None


def _reconcile_live_positions(db, brokers: dict) -> None:
    """
    Safety net for the crash window described in
    utils/position_reconciliation.py's module docstring: this app places
    a real LIVE order BEFORE committing the matching DB row, so a process
    death in between can leave the broker holding a real position this
    app has no record of (or the mirror case on exit). Compares this
    app's OPEN mode='live' CustomStrategyPosition rows against the real
    broker's actual net positions and ALERTS (never auto-corrects — a
    wrong automatic fix on a live account is worse than a detected,
    human-reviewed drift) on any mismatch. PAPER mode has no real
    exchange to reconcile against and is out of scope here.
    """
    from utils.position_reconciliation import reconcile_live_positions

    live_broker = brokers.get("live")
    if live_broker is None:
        return

    live_legs = db.query(CustomStrategyPosition).filter(
        CustomStrategyPosition.mode == "live", CustomStrategyPosition.status == "OPEN",
    ).all()
    if not live_legs:
        return

    broker_net = live_broker.get_broker_positions()
    mismatches = reconcile_live_positions(
        [{"instrument_key": leg.instrument_key, "transaction_type": leg.transaction_type, "quantity": leg.quantity} for leg in live_legs],
        broker_net,
    )
    if mismatches is None:
        log.warning("custom_strategy_scheduler: position reconciliation skipped this cycle — could not fetch broker positions.")
        return
    if not mismatches:
        return

    log.critical("custom_strategy_scheduler: LIVE POSITION RECONCILIATION MISMATCH: %s", mismatches)

    strategy_ids = {leg.strategy_id for leg in live_legs}
    strategies = {s.id: s for s in db.query(CustomStrategy).filter(CustomStrategy.id.in_(strategy_ids)).all()}
    legs_by_instrument: dict[str, list] = defaultdict(list)
    for leg in live_legs:
        legs_by_instrument[leg.instrument_key].append(leg)

    # Group by owning user (via each mismatched instrument's leg(s) ->
    # strategy -> user_id) so each account only ever sees its own drift —
    # a mismatch with NO matching DB leg at all (a fully orphaned broker
    # position) has no owner to attribute it to and goes out system-wide.
    mismatches_by_user: dict[int | None, list] = defaultdict(list)
    for m in mismatches:
        owning_legs = legs_by_instrument.get(m["instrument_key"], [])
        user_ids = {strategies[leg.strategy_id].user_id for leg in owning_legs if leg.strategy_id in strategies} or {None}
        for uid in user_ids:
            mismatches_by_user[uid].append(m)

    for user_id, user_mismatches in mismatches_by_user.items():
        lines = "\n".join(
            f"  {m['instrument_key']}: app expects net qty {m['db_quantity']}, broker shows {m['broker_quantity']}"
            for m in user_mismatches
        )
        notify(
            "custom_strategy",
            f"LIVE position mismatch detected between this app's records and your real Upstox account:\n{lines}\n"
            f"This can happen if the app restarted mid-order, or a position changed outside the app. "
            f"Nothing was auto-corrected — please verify your actual positions manually.",
            level="error",
            user_id=user_id,
        )


def _send_pnl_updates(db, brokers: dict) -> None:
    """
    Periodic snapshot of every currently-OPEN basket's live P&L — called
    from api/strategy_scheduler.py at a deliberately coarse cadence
    (_PNL_UPDATE_INTERVAL_SEC there), not per-tick — a per-tick P&L push
    would be spam, not a notification anyone reads. One notification per
    strategy (not per leg) — legs are
    grouped and combined the same way _try_exit's TP/SL check does, so
    the % shown here is the exact number that would trigger an exit.
    """
    from utils.wallet import get_charge_rates

    open_legs = db.query(CustomStrategyPosition).filter(CustomStrategyPosition.status == "OPEN").all()
    if not open_legs:
        return

    legs_by_strategy = defaultdict(list)
    for leg in open_legs:
        legs_by_strategy[leg.strategy_id].append(leg)

    strategies = {
        s.id: s for s in db.query(CustomStrategy).filter(CustomStrategy.id.in_(legs_by_strategy.keys())).all()
    }

    for strategy_id, legs in legs_by_strategy.items():
        strategy = strategies.get(strategy_id)
        if strategy is None:
            continue
        mode = legs[0].mode
        broker = brokers.get(mode)
        if broker is None:
            continue

        tokens = [leg.instrument_key for leg in legs]
        try:
            now_prices = broker.get_ltp_batch(tokens)
        except Exception as exc:
            log.warning("custom_strategy_scheduler: pnl update LTP fetch failed for strategy %s: %s", strategy_id, exc)
            continue

        rates = get_charge_rates(strategy.user_id)
        pnl_pct = _combined_pnl_pct(legs, now_prices, rates)
        if pnl_pct is None:
            continue

        symbols = json.loads(strategy.symbols)
        symbol = next((s for s in symbols if any(_is_leg_for_symbol(leg.instrument_key, s) for leg in legs)), symbols[0] if symbols else "?")
        leg_details = " | ".join(
            f"{leg.transaction_type} {leg.option_type} {leg.strike}@{now_prices.get(leg.instrument_key, 0) or 0:.2f}"
            for leg in legs
        )
        details = f"{leg_details} | pnl={pnl_pct:+.2f}%"
        notify("custom_strategy", f"P&L Update — {strategy.name}\n{symbol} ({mode})\n{details}",
               level="pnl", user_id=strategy.user_id)
        alert_pnl_update(strategy.name, mode, symbol, details)


def _tick_one_strategy(db, strategy: CustomStrategy, brokers: dict) -> None:
    """
    One strategy's worth of a scheduler tick — extracted from the main
    loop below so it's directly unit-testable. Never raises (logs and
    returns) — the caller ticks many strategies per pass and one
    strategy's failure must not stop the rest.
    """
    if not strategy.rules_json:
        return

    if strategy.status == "PAUSED":
        # No new entries while paused — but any already-open leg must
        # keep being exit-managed (see module docstring). The strategy's
        # own status is mode-ambiguous once paused, so the broker is
        # derived from the open legs' own recorded mode instead of
        # _mode_for_status(status).
        first_leg_mode = db.query(CustomStrategyPosition.mode).filter(
            CustomStrategyPosition.strategy_id == strategy.id,
            CustomStrategyPosition.status == "OPEN",
        ).first()
        if first_leg_mode is None:
            return  # nothing open — nothing to do while paused
        try:
            _try_exit(db, strategy, brokers[first_leg_mode[0]])
        except Exception as exc:
            log.error("custom_strategy_scheduler: paused-strategy exit tick failed for strategy %s: %s", strategy.id, exc, exc_info=True)
        return

    broker = brokers[_mode_for_status(strategy.status)]
    try:
        _try_exit(db, strategy, broker)
        _try_entry(db, strategy, broker)
    except Exception as exc:
        log.error("custom_strategy_scheduler: tick failed for strategy %s: %s", strategy.id, exc, exc_info=True)
