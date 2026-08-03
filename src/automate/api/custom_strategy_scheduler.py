"""
api/custom_strategy_scheduler.py — runs paper/live execution for
user-built custom strategies (CustomStrategy rows with status
PAPER_TRADING, LIVE, or PAUSED) as a background asyncio task inside the
SAME FastAPI process (started from main.py's startup hook, exactly like
market_broadcaster.py/the daemon supervisor) — per the standing rule in
this codebase: no second systemd service for background work.

This is deliberately independent of run_daemon.py/run_strategy.py (the
.env/STRATEGY_CONFIGS-driven system for hand-written strategies like
TenPercentOTMStrangle) — those are keyed by a static strategy name, not a
DB row a user can create/edit/delete at runtime. Custom strategies get
their own lightweight scheduler here instead of being forced through
that static config path.

Each tick (every _TICK_SEC, only during real NSE market hours):
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
import asyncio
import json
import threading
import time
from datetime import date, datetime
from typing import Dict, Optional
from zoneinfo import ZoneInfo

from automate.compliance.sebi_rules import AuditTrail, KillSwitch, OrderRateLimiter, assert_market_is_open
from automate.db.engine import SessionLocal
from automate.db.models import CustomStrategy, CustomStrategyPosition
from automate.strategies.custom.rule_strategy import RuleBasedStrategy
from automate.utils.instrument_cache import InstrumentCache
from automate.utils.logger import get_logger
from automate.utils.notify import notify
from automate.utils.option_utils import check_exit_trigger, is_within_pre_expiry_buffer
from automate.utils.trailing_stop import advance_trailing_stop, stop_triggered
from sqlalchemy import text, update

log = get_logger(__name__)

_TICK_SEC_OPEN = 60
_TICK_SEC_CLOSED = 300

# How often to run the LIVE-position reconciliation safety net (see
# utils/position_reconciliation.py) — deliberately much coarser than the
# trading tick itself; it exists to catch drift from a rare crash-window
# event, not to run on every tick and hammer the broker's positions API.
_RECONCILE_INTERVAL_SEC = 600
_last_reconcile_at: Optional[float] = None

# entry/exit "time" rules (e.g. "10:00") are entered by the user as NSE
# market-hours-of-day (IST) — the server itself runs on UTC, so comparing
# them against a naive datetime.now() would silently compare against the
# wrong clock (see _try_entry/_try_exit below, which is the whole reason
# this exists — sebi_rules.assert_market_is_open already gets this right).
_IST = ZoneInfo("Asia/Kolkata")

_audit = AuditTrail(audit_log_path="logs/custom_strategy_audit.log")
_kill_switch = KillSwitch()
_rate_limiter = OrderRateLimiter(max_per_second=10)
_brokers: Optional[dict] = None
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


def _get_brokers() -> Optional[dict]:
    """Lazily build the paper/live broker pair; retried every tick until it succeeds (e.g. token not ready yet)."""
    global _brokers
    with _brokers_lock:
        if _brokers is not None:
            return _brokers
        try:
            from automate.broker.broker_factory import BrokerFactory
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


def _get_last_entered_expiry(strategy: CustomStrategy, symbol: str, mode: str) -> Optional[str]:
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


def _set_last_entered_expiry(strategy: CustomStrategy, symbol: str, mode: str, expiry: str) -> None:
    try:
        data = json.loads(strategy.last_entry_date) if strategy.last_entry_date else {}
        if not isinstance(data, dict):
            data = {}
    except json.JSONDecodeError:
        data = {}
    symbol_entry = data.get(symbol)
    if not isinstance(symbol_entry, dict):
        symbol_entry = {}
    symbol_entry[mode] = {"expiry": expiry, "date": date.today().isoformat()}
    data[symbol] = symbol_entry
    strategy.last_entry_date = json.dumps(data)


def _resolve_current_expiry(broker, symbol: str, mode: str) -> Optional[str]:
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

    from automate.utils.option_utils import find_nearest_expiry_by_type

    try:
        instrument_key = broker.resolve_instrument_key(symbol)
        expiries = broker.get_option_contracts(instrument_key)
        if not expiries:
            return None
        now = broker.get_current_time()
        return find_nearest_expiry_by_type(expiries, mode, reference_date=now.date() if now else None)
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

    from automate.utils.iv_rank import compute_iv_rank
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


def _try_entry(db, strategy: CustomStrategy, broker) -> None:
    symbols = json.loads(strategy.symbols)
    rules = json.loads(strategy.rules_json)
    entry_rule = rules.get("entry") or {}
    entry_mode = entry_rule.get("mode")

    if entry_mode == "AT_TIME":
        target = entry_rule.get("time")
        now_hhmm = datetime.now(_IST).strftime("%H:%M")
        if now_hhmm < target:
            return

    # Fetch all open positions for this strategy
    open_positions = db.query(CustomStrategyPosition).filter(
        CustomStrategyPosition.strategy_id == strategy.id,
        CustomStrategyPosition.status == "OPEN",
    ).all()

    # {expiry_mode: [leg_index, ...]} — one group for a normal (single-
    # expiry) strategy, 2+ for a calendar spread. See _leg_groups().
    groups = _leg_groups(rules)

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
            current_expiry = _resolve_current_expiry(broker, symbol, mode)
            if current_expiry is None:
                continue  # Couldn't resolve (broker hiccup) — retry next tick, don't mark anything.
            if _get_last_entered_expiry(strategy, symbol, mode) == current_expiry:
                continue  # Already traded this cycle.

            try:
                rule_strategy = RuleBasedStrategy(
                    broker=broker, audit=_audit, kill_switch=_kill_switch, rate_limiter=_rate_limiter,
                    symbol=symbol, rules=rules, user_id=strategy.user_id,
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
            _set_last_entered_expiry(strategy, symbol, mode, entered_expiry)

            db_mode = _mode_for_status(strategy.status)
            for original_idx, leg in zip(result["leg_indices"], result["legs"]):
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


def _combined_pnl_pct(legs: list[CustomStrategyPosition], now_prices: dict, rates: Optional[dict] = None) -> Optional[float]:
    """
    NET P&L (of the combined premium at entry) — includes real Upstox
    transaction costs (brokerage/STT/exchange charges/GST/SEBI charges/
    stamp duty, see utils/costs.py) on both the original entry AND the
    hypothetical exit at `now_prices`, same as backtest/custom_engine.py
    computes for historical cycles. Slippage is already baked into
    leg.entry_price/now_prices themselves (PaperBroker/UpstoxBroker apply
    it at fill time) — this function only adds the fee layer backtest
    already had and paper/live previously didn't, so the two modes are
    now comparable apples-to-apples.

    Used both for the live TP/SL trigger check (so a "+40% profit"
    target is evaluated net of real costs, not a false-positive gross
    number) and for the final stored paper_return_pct/live_return_pct.
    """
    from automate.utils.costs import calculate_options_transaction_cost_breakdown

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
        entry_costs = calculate_options_transaction_cost_breakdown(entry, qty, leg.transaction_type, rates)
        exit_costs = calculate_options_transaction_cost_breakdown(now, qty, exit_transaction_type, rates)
        pnl_amount -= entry_costs.get("total", 0) + exit_costs.get("total", 0)
    return (pnl_amount / denom * 100.0) if denom > 0 else 0.0


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
        exit_order_id = opposite(
            instrument_token=leg.instrument_key, quantity=leg.quantity, order_type="MARKET",
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
    leg.exit_price = now_prices.get(leg.instrument_key)
    leg.exit_order_id = exit_order_id
    leg.exit_reason = trigger
    leg.closed_at = datetime.now()
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

    from automate.utils.wallet import get_charge_rates
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


def _try_exit(db, strategy: CustomStrategy, broker) -> None:
    legs = db.query(CustomStrategyPosition).filter(
        CustomStrategyPosition.strategy_id == strategy.id,
        CustomStrategyPosition.status == "OPEN",
    ).all()
    if not legs:
        return

    from automate.utils.wallet import get_charge_rates
    rates = get_charge_rates(strategy.user_id)

    rules = json.loads(strategy.rules_json)
    exit_rule = rules.get("exit") or {}
    take_profit_pct = exit_rule.get("take_profit_pct")
    stop_loss_pct = exit_rule.get("stop_loss_pct")
    exit_time = exit_rule.get("exit_time")
    exit_days_before_expiry = exit_rule.get("exit_days_before_expiry", 0)

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
        combined_managed = [l for l in still_open if not l.leg_config_json]
        pnl_pct = _combined_pnl_pct(combined_managed, now_prices, rates) if combined_managed else None
        trigger = check_exit_trigger(pnl_pct, take_profit_pct, stop_loss_pct) if pnl_pct is not None else None

        # 3. Strategy-level time/expiry — a calendar "hard stop" that
        #    still applies to EVERY still-open leg of this symbol,
        #    including individually-managed ones that survived step 1
        #    (a leg with its own trailing stop shouldn't stay open past
        #    the strategy's own expiry cutoff).
        hard_stop = None
        if exit_time and datetime.now(_IST).strftime("%H:%M") >= exit_time:
            hard_stop = "TIME_EXIT"
        if hard_stop is None and exit_days_before_expiry:
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
        combined_closed = [l for l in closed_legs if l in combined_managed]

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


def dtime_or_none(expiry_str: Optional[str]):
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
    from automate.utils.position_reconciliation import reconcile_live_positions

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
        [{"instrument_key": l.instrument_key, "transaction_type": l.transaction_type, "quantity": l.quantity} for l in live_legs],
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
    legs_by_instrument: Dict[str, list] = defaultdict(list)
    for leg in live_legs:
        legs_by_instrument[leg.instrument_key].append(leg)

    # Group by owning user (via each mismatched instrument's leg(s) ->
    # strategy -> user_id) so each account only ever sees its own drift —
    # a mismatch with NO matching DB leg at all (a fully orphaned broker
    # position) has no owner to attribute it to and goes out system-wide.
    mismatches_by_user: Dict[Optional[int], list] = defaultdict(list)
    for m in mismatches:
        owning_legs = legs_by_instrument.get(m["instrument_key"], [])
        user_ids = {strategies[l.strategy_id].user_id for l in owning_legs if l.strategy_id in strategies} or {None}
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


async def custom_strategy_scheduler() -> None:
    """Persistent background task — see module docstring. Never raises; logs and keeps ticking."""
    from automate.utils.single_instance_lock import acquire_singleton_lock

    if not acquire_singleton_lock("custom_strategy_scheduler"):
        log.critical(
            "custom_strategy_scheduler: another instance already holds this lock — refusing to start a "
            "second trading loop (it would independently double-place every live order). If you're SURE "
            "no other instance of this app is actually running, delete "
            "logs/custom_strategy_scheduler.lock and restart."
        )
        return

    log.info("custom_strategy_scheduler: started.")
    while True:
        try:
            market_open = _market_is_open_now()
            if market_open:
                brokers = _get_brokers()
                if brokers is not None:
                    db = SessionLocal()
                    try:
                        strategies = db.query(CustomStrategy).filter(
                            CustomStrategy.status.in_(["PAPER_TRADING", "LIVE", "PAUSED"])
                        ).all()
                        for strategy in strategies:
                            _tick_one_strategy(db, strategy, brokers)

                        global _last_reconcile_at
                        now_monotonic = time.monotonic()
                        if _last_reconcile_at is None or now_monotonic - _last_reconcile_at >= _RECONCILE_INTERVAL_SEC:
                            _last_reconcile_at = now_monotonic
                            try:
                                _reconcile_live_positions(db, brokers)
                            except Exception as exc:
                                log.error("custom_strategy_scheduler: position reconciliation failed: %s", exc, exc_info=True)
                    finally:
                        db.close()
        except Exception as exc:
            log.error("custom_strategy_scheduler: tick-level failure: %s", exc, exc_info=True)

        await asyncio.sleep(_TICK_SEC_OPEN if market_open else _TICK_SEC_CLOSED)
