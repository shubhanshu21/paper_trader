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
from api.matrix_calendar_engine import _tick_one_strategy as _tick_matrix_calendar
from api.nine_fifteen_engine import _tick_one_strategy as _tick_nine_fifteen
from api.otm_put_roll_engine import _tick_one_strategy as _tick_otm_put_roll
from api.session_seller_engine import _tick_one_strategy as _tick_session_seller
from api.smart_condor_engine import _tick_one_strategy as _tick_smart_condor
from api.weekend_combo_scheduler import _tick_one_strategy as _tick_combo
from api.weekly_directional_engine import _tick_one_strategy as _tick_weekly_directional
from api.zero_to_hero_engine import _tick_one_strategy as _tick_zero_to_hero
from compliance.sebi_rules import assert_market_is_open, get_global_kill_switch
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
_DRAWDOWN_CHECK_INTERVAL_SEC = 300  # a safety check, checked more often than reconcile/pnl-update
_last_reconcile_at: float | None = None
_last_pnl_update_at: float | None = None
_last_drawdown_check_at: float | None = None

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
    "WEEKLY_DIRECTIONAL": _tick_weekly_directional,
    "MATRIX_CALENDAR": _tick_matrix_calendar,
    "ZERO_TO_HERO": _tick_zero_to_hero,
    "NINE_FIFTEEN_ORB": _tick_nine_fifteen,
}


def _market_is_open_now() -> bool:
    try:
        assert_market_is_open()
        return True
    except RuntimeError:
        return False


def _check_drawdown_auto_trigger(db) -> None:
    """
    Auto-trips the GLOBAL kill switch once any user's today's realized
    LIVE P&L breaches their own configured max_daily_drawdown_pct (opt-
    in, WalletSettings.max_daily_drawdown_pct — NULL means no auto-
    trigger for that user, the default). Real per-leg P&L only (no
    fabricated MTM on still-open legs) — reuses utils.pnl.compute_
    basket_pnl, the same net-of-real-charges calculation the Leaderboard/
    Trade Journal already use.

    There is only ONE global switch (see compliance/sebi_rules.py's own
    docstring on why 11 separate per-engine instances were themselves a
    bug) — tripping it here halts every strategy's new order flow for
    EVERY user, not just the one whose threshold fired. That's the
    correct scope for an emergency stop: SEBI's own kill-switch mandate
    describes halting ALL order generation, not a scoped subset.
    """
    from datetime import date

    from db.models import CustomStrategyPosition, WalletSettings
    from utils.notify import notify
    from utils.pnl import compute_basket_pnl
    from utils.wallet import get_charge_rates

    kill_switch = get_global_kill_switch()
    if kill_switch.is_active():
        return  # already tripped (manually or by an earlier check this tick) — nothing further to do

    configured = db.query(WalletSettings).filter(WalletSettings.max_daily_drawdown_pct.isnot(None)).all()
    if not configured:
        return

    today = date.today()
    for wallet in configured:
        if not wallet.user_id or not wallet.starting_capital:
            continue
        own_strategy_ids = {r[0] for r in db.query(CustomStrategy.id).filter(CustomStrategy.user_id == wallet.user_id).all()}
        if not own_strategy_ids:
            continue
        legs = db.query(CustomStrategyPosition).filter(
            CustomStrategyPosition.strategy_id.in_(own_strategy_ids),
            CustomStrategyPosition.mode == "live",
            CustomStrategyPosition.status == "CLOSED",
        ).all()
        todays_legs = [leg for leg in legs if leg.closed_at and leg.closed_at.date() == today and leg.exit_price is not None]
        if not todays_legs:
            continue

        rates = get_charge_rates(wallet.user_id)
        result = compute_basket_pnl([
            {"entry_price": leg.entry_price, "exit_price": leg.exit_price, "quantity": leg.quantity,
             "transaction_type": leg.transaction_type, "instrument_type": leg.instrument_type}
            for leg in todays_legs
        ], rates)
        drawdown_pct = 100 * result["net_pnl"] / float(wallet.starting_capital)
        threshold = -abs(float(wallet.max_daily_drawdown_pct))

        if drawdown_pct <= threshold:
            reason = (
                f"Auto-triggered: user {wallet.user_id}'s today's realized LIVE P&L is "
                f"{drawdown_pct:.2f}% of starting capital, past their configured {threshold:.2f}% daily drawdown limit."
            )
            kill_switch.activate(reason=reason)
            log.critical("strategy_scheduler: AUTO KILL SWITCH — %s", reason)
            notify(
                "kill_switch",
                f"🚨 GLOBAL KILL SWITCH AUTO-ACTIVATED — {reason} ALL new order flow (every strategy, every "
                f"user) is halted until manually reset from the Kill Switch control. Already-open positions "
                f"can still be closed.",
                level="error", user_id=wallet.user_id,
            )
            return  # one trip halts everything — no need to keep checking other users this tick


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
        # Set before the try so a failure anywhere inside it (including
        # _market_is_open_now() itself) can't leave this unbound — it's
        # read below, outside the try, to pick the sleep interval.
        market_open = False
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

                        global _last_reconcile_at, _last_pnl_update_at, _last_drawdown_check_at
                        now_monotonic = time.monotonic()
                        if _last_drawdown_check_at is None or now_monotonic - _last_drawdown_check_at >= _DRAWDOWN_CHECK_INTERVAL_SEC:
                            _last_drawdown_check_at = now_monotonic
                            try:
                                _check_drawdown_auto_trigger(db)
                            except Exception as exc:
                                log.error("strategy_scheduler: drawdown auto-trigger check failed: %s", exc, exc_info=True)

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
