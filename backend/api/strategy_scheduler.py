"""
api/strategy_scheduler.py — the single background asyncio task that ticks
every CustomStrategy row (any strategy_type) once per pass, dispatching
each row to its own engine's _tick_one_strategy implementation.

Replaces three formerly-separate loops — custom_strategy_scheduler(),
intraday_indicator_scheduler(), weekend_combo_scheduler() — that existed
because each strategy shape (fixed legs/once-per-expiry-cycle vs.
signal-driven/multiple-entries-per-day vs. two-symbols-combined-once-a-
week) is a genuinely different execution model. The per-engine helper
functions those three modules define (_try_entry/_try_exit,
_tick_one_strategy, _get_brokers, _combined_pnl, etc.) are UNCHANGED and
imported from here as-is — this file only unifies the scheduling loop
itself (market-hours check, DB query, broker fetch, dispatch), not the
trading logic. See strategies/custom/engine_registry.py for the matching
route-layer dispatch (validate/describe/backtest-support).

A real side-effect worth knowing: _reconcile_live_positions/
_send_pnl_updates (custom_strategy_scheduler.py) query
CustomStrategyPosition with no strategy_type filter at all — they were
already type-agnostic, just never called from the other two engines'
loops. Running them from this single shared loop means SUPERTREND_INTRADAY/
WEEKEND_GAP_COMBO live positions now get the same reconciliation safety
net and periodic P&L push every CUSTOM strategy already had.
"""
import asyncio
import time

# Reused as-is — NOT reimplemented — from each engine's own module.
from api.custom_strategy_scheduler import (
    _get_brokers,
    _reconcile_live_positions,
    _send_pnl_updates,
)
from api.custom_strategy_scheduler import (
    _tick_one_strategy as _tick_custom,
)
from api.delta_neutral_engine import _tick_one_strategy as _tick_delta_neutral
from api.gravity_engine import _tick_one_strategy as _tick_gravity
from api.intraday_indicator_scheduler import _tick_one_strategy as _tick_intraday
from api.macd_credit_engine import _tick_one_strategy as _tick_macd_credit
from api.otm_put_roll_engine import _tick_one_strategy as _tick_otm_put_roll
from api.session_seller_engine import _tick_one_strategy as _tick_session_seller
from api.smart_condor_engine import _tick_one_strategy as _tick_smart_condor
from api.weekend_combo_scheduler import _tick_one_strategy as _tick_combo
from compliance.sebi_rules import assert_market_is_open
from db.engine import SessionLocal
from db.models import CustomStrategy
from utils.logger import get_logger

log = get_logger(__name__)

# The finer of the two intervals the three old loops used (leg-based was
# 60/300, the other two were 30/300) — the Supertrend engine's whole value
# is reacting to a live signal quickly, so that can't regress to 60s.
# Ticking the leg-based/combo engines at 30s instead of 60s is free: every
# entry/exit check either makes is an idempotent gate ("already traded
# this cycle?", a TP/SL/time threshold) — ticking faster only means
# reacting to a trigger sooner, never a different decision.
_TICK_SEC_OPEN = 30
_TICK_SEC_CLOSED = 300
_RECONCILE_INTERVAL_SEC = 600   # moved from custom_strategy_scheduler.py
_PNL_UPDATE_INTERVAL_SEC = 900  # moved from custom_strategy_scheduler.py
_last_reconcile_at: float | None = None
_last_pnl_update_at: float | None = None

# strategy_type -> _tick_one_strategy implementation. Any type NOT in this
# dict (CUSTOM, legacy STRADDLE/STRANGLE/etc, None) falls through to the
# leg-based default — same "only genuinely new execution models need an
# entry here" rule engine_registry.py's get_engine() follows.
_TICK_FUNCS = {
    "SUPERTREND_INTRADAY": _tick_intraday,
    "WEEKEND_GAP_COMBO": _tick_combo,
    "OTM_PUT_ROLL": _tick_otm_put_roll,
    "SMART_CONDOR": _tick_smart_condor,
    "GRAVITY": _tick_gravity,
    "SESSION_SELLER": _tick_session_seller,
    "MACD_CREDIT_SPREAD": _tick_macd_credit,
    "DELTA_NEUTRAL_STRANGLE": _tick_delta_neutral,
}


def _market_is_open_now() -> bool:
    try:
        assert_market_is_open()
        return True
    except RuntimeError:
        return False


async def strategy_scheduler() -> None:
    """Persistent background task — see module docstring. Never raises; logs and keeps ticking."""
    from utils.single_instance_lock import acquire_singleton_lock

    if not acquire_singleton_lock("strategy_scheduler"):
        log.critical(
            "strategy_scheduler: another instance already holds this lock — refusing to start a second "
            "trading loop (it would independently double-place every live order). If you're SURE no other "
            "instance of this app is actually running, delete logs/strategy_scheduler.lock and restart."
        )
        return

    log.info("strategy_scheduler: started.")
    while True:
        try:
            market_open = _market_is_open_now()
            if market_open:
                brokers = _get_brokers()
                if brokers is not None:
                    db = SessionLocal()
                    try:
                        strategies = db.query(CustomStrategy).filter(
                            CustomStrategy.status.in_(["PAPER_TRADING", "LIVE", "PAUSED"]),
                        ).all()
                        for strategy in strategies:
                            tick_fn = _TICK_FUNCS.get(strategy.strategy_type, _tick_custom)
                            try:
                                tick_fn(db, strategy, brokers)
                            except Exception as exc:
                                # Every engine's own _tick_one_strategy is documented to "never
                                # raise" for its OWN known failure modes (a bad order, an
                                # unresolvable strike, etc — each catches those internally and
                                # logs/notifies). This is the backstop for anything that slips
                                # past that discipline — e.g. broker.get_ltp() raising
                                # RuntimeError after exhausting its retries (its own docstring
                                # says it returns None on failure; the real implementation does
                                # not) — a real gap no engine's get_ltp() call sites guard
                                # against. Without this, one strategy's transient failure (a
                                # rate-limit burst right after startup, in practice) would skip
                                # EVERY strategy queried after it this tick, not just itself.
                                db.rollback()
                                log.error(
                                    "strategy_scheduler: strategy %s (%s, %s) failed this tick: %s",
                                    strategy.id, strategy.name, strategy.strategy_type, exc, exc_info=True,
                                )

                        global _last_reconcile_at, _last_pnl_update_at
                        now_monotonic = time.monotonic()
                        if _last_reconcile_at is None or now_monotonic - _last_reconcile_at >= _RECONCILE_INTERVAL_SEC:
                            _last_reconcile_at = now_monotonic
                            try:
                                _reconcile_live_positions(db, brokers)
                            except Exception as exc:
                                log.error("strategy_scheduler: position reconciliation failed: %s", exc, exc_info=True)

                        if _last_pnl_update_at is None or now_monotonic - _last_pnl_update_at >= _PNL_UPDATE_INTERVAL_SEC:
                            _last_pnl_update_at = now_monotonic
                            try:
                                _send_pnl_updates(db, brokers)
                            except Exception as exc:
                                log.error("strategy_scheduler: pnl update failed: %s", exc, exc_info=True)
                    finally:
                        db.close()
        except Exception as exc:
            log.error("strategy_scheduler: tick-level failure: %s", exc, exc_info=True)

        await asyncio.sleep(_TICK_SEC_OPEN if market_open else _TICK_SEC_CLOSED)
