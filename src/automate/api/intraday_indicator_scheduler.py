"""
api/intraday_indicator_scheduler.py — runs paper/live execution for
Supertrend+Pivot intraday strategies (CustomStrategy rows with
strategy_type == "SUPERTREND_INTRADAY", see strategies/custom/
intraday_schema.py) as a background asyncio task, same "no second
systemd service for background work" rule as custom_strategy_scheduler.py.

This is a SEPARATE scheduler from custom_strategy_scheduler.py, not an
extension of it, because this strategy shape breaks that scheduler's
core assumption: "enter once per expiry cycle, hold, exit once."
Supertrend+Pivot strategies can enter/exit several INDEPENDENT times
within a single trading day, with the option_type decided live by a
signal rather than fixed at build time. custom_strategy_scheduler.py's
own main query explicitly excludes strategy_type == "SUPERTREND_INTRADAY"
so these rows are never double-processed by both loops.

Each tick (every _TICK_SEC, only during real NSE market hours):
  1. EXIT — for every strategy with an OPEN position: close it if the
     Supertrend has flipped away from the trend it was entered on, or if
     the configured exit_time has been reached (no overnight positions,
     ever — this fires regardless of PAUSED/signal state, same hard-stop
     precedence exit_time has in custom_strategy_scheduler.py).
  2. ENTRY — for every PAPER_TRADING/LIVE strategy (PAUSED strategies are
     skipped for entry, same convention as custom_strategy_scheduler.py)
     with NO open position, before exit_time, and under today's
     max_trades_per_day cap: evaluate the live signal and SELL the ATM
     PUT (bullish) or ATM CALL (bearish) if one fired. "Today's trade
     count" is COUNT(*) of this strategy's CustomStrategyPosition rows
     opened today — no separate counter column needed, opened_at already
     exists on that model.
"""
import asyncio
import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func

from automate.compliance.sebi_rules import AuditTrail, ComplianceError, KillSwitch, OrderRateLimiter, assert_market_is_open
from automate.db.engine import SessionLocal
from automate.db.models import CustomStrategy, CustomStrategyPosition
from automate.strategies.custom.intraday_indicator_strategy import IntradaySupertrendStrategy
from automate.strategies.custom.intraday_schema import get_setting
from automate.utils.logger import get_logger
from automate.utils.notify import notify
from automate.utils.telegram_alert import alert_trade_closed, alert_trade_opened

log = get_logger(__name__)

_TICK_SEC_OPEN = 30
_TICK_SEC_CLOSED = 300
_IST = ZoneInfo("Asia/Kolkata")

_audit = AuditTrail(audit_log_path="logs/intraday_indicator_audit.log")
_kill_switch = KillSwitch()
_rate_limiter = OrderRateLimiter(max_per_second=10)


def _market_is_open_now() -> bool:
    try:
        assert_market_is_open()
        return True
    except RuntimeError:
        return False


def _mode_for_status(status: str) -> str:
    return "paper" if status == "PAPER_TRADING" else "live"


def _get_brokers() -> dict | None:
    """Reuses the SAME cached broker pair custom_strategy_scheduler.py builds — one live/paper broker pair per process, not a second independent one for this scheduler."""
    from automate.api.custom_strategy_scheduler import _get_brokers as _shared_get_brokers
    return _shared_get_brokers()


def _today_trade_count(db, strategy_id: int) -> int:
    """
    How many entries this strategy has already made TODAY, regardless of
    whether they're still open — the max_trades_per_day cap counts
    ATTEMPTS made, not concurrently open positions (this strategy only
    ever holds one position at a time anyway). Uses the DB's own DATE()
    on opened_at rather than a Python-side IST/UTC midnight computation,
    so "today" always matches whatever the DB server's own clock/session
    timezone considers today — no separate timezone assumption to get
    wrong here.
    """
    return db.query(CustomStrategyPosition).filter(
        CustomStrategyPosition.strategy_id == strategy_id,
        func.date(CustomStrategyPosition.opened_at) == date.today(),
    ).count()


def _close_position(db, strategy: CustomStrategy, broker, position: CustomStrategyPosition, trigger: str, now_price: float | None, symbol: str) -> bool:
    """
    Square off ONE open intraday position — opposite MARKET order, same
    side-effects (audit, notify, atomic OPEN->CLOSING claim to prevent a
    double-close race) as custom_strategy_scheduler.py's own _close_leg,
    reimplemented here rather than imported since this scheduler's
    strategies are always exactly one SELL leg (no auto-unwind/partial-
    fill complexity to share) — see that module's _close_leg for the
    fuller N-leg version this mirrors.
    """
    from sqlalchemy import update

    claimed = db.execute(
        update(CustomStrategyPosition)
        .where(CustomStrategyPosition.id == position.id, CustomStrategyPosition.status == "OPEN")
        .values(status="CLOSING")
    ).rowcount
    db.commit()
    if claimed == 0:
        return False

    try:
        _rate_limiter.acquire()
        exit_order_id = broker.place_buy_order(
            instrument_token=position.instrument_key, quantity=position.quantity, product="MIS",
            order_type="MARKET", tag=f"INTRADAY_EXIT_{strategy.id}"[:20], user_id=strategy.user_id,
        )
    except Exception as exc:
        log.critical(
            "intraday_indicator_scheduler: FAILED to square off position %s for strategy %s: %s — MANUAL INTERVENTION REQUIRED.",
            position.instrument_key, strategy.id, exc,
        )
        notify(
            "custom_strategy",
            f"MANUAL INTERVENTION REQUIRED — failed to exit \"{strategy.name}\" position {position.instrument_key} "
            f"({trigger} triggered): {exc}. A position may still be open on your real account.",
            user_id=strategy.user_id,
        )
        position.status = "OPEN"  # release the claim so the next tick retries
        db.commit()
        return False

    position.status = "CLOSED"
    fill_price = broker.get_fill_price(exit_order_id) if exit_order_id else None
    position.exit_price = fill_price if fill_price is not None else now_price
    position.exit_order_id = exit_order_id
    position.exit_reason = trigger
    position.closed_at = datetime.now()
    db.commit()

    mode = _mode_for_status(strategy.status)
    details = f"reason={trigger} | SELL {position.option_type} {position.strike}@{position.exit_price or 0:.2f}"
    notify("custom_strategy", f"Trade Closed — {strategy.name}\n({mode})\n{details}", level="trade", user_id=strategy.user_id)
    alert_trade_closed(strategy.name, mode, symbol, details)
    return True


def _tick_one_strategy(db, strategy: CustomStrategy, brokers: dict) -> None:
    """One strategy's worth of a tick. Never raises — logs and returns, same discipline as custom_strategy_scheduler.py's _tick_one_strategy, so one strategy's failure never stops the rest."""
    if not strategy.rules_json:
        return
    try:
        rules = json.loads(strategy.rules_json)
        symbols = json.loads(strategy.symbols)
        symbol = symbols[0]
    except (json.JSONDecodeError, IndexError):
        return

    exit_time = get_setting(rules, "exit_time")
    max_trades = get_setting(rules, "max_trades_per_day")

    # Position lookup — this scheduler only ever opens ONE position at a
    # time per strategy (leg_index always 0), so "the open position" is
    # unambiguous, unlike the N-leg baskets custom_strategy_scheduler.py
    # manages.
    open_position = db.query(CustomStrategyPosition).filter(
        CustomStrategyPosition.strategy_id == strategy.id,
        CustomStrategyPosition.status == "OPEN",
    ).first()

    mode = "paused" if strategy.status == "PAUSED" else _mode_for_status(strategy.status)
    broker_mode = open_position.mode if (strategy.status == "PAUSED" and open_position) else _mode_for_status(strategy.status)
    broker = brokers.get(broker_mode)
    if broker is None:
        return

    now_hhmm = datetime.now(_IST).strftime("%H:%M")

    try:
        engine = IntradaySupertrendStrategy(
            broker=broker, audit=_audit, kill_switch=_kill_switch, rate_limiter=_rate_limiter,
            symbol=symbol, rules=rules, user_id=strategy.user_id,
        )
    except Exception as exc:
        log.error("intraday_indicator_scheduler: could not build engine for strategy %s (%s): %s", strategy.id, strategy.name, exc)
        return

    # 1. EXIT — time hard-stop takes precedence over everything else (no
    #    overnight positions, ever), then a genuine Supertrend flip.
    if open_position is not None:
        now_price = broker.get_ltp(open_position.instrument_key)
        if now_hhmm >= exit_time:
            _close_position(db, strategy, broker, open_position, "TIME_EXIT", now_price, symbol)
            return  # done for this tick either way — don't also try to re-enter below

        signal = engine.evaluate_signal()
        if signal is not None:
            entry_config = json.loads(open_position.leg_config_json) if open_position.leg_config_json else {}
            entry_trend = entry_config.get("entry_trend")
            if entry_trend is not None and signal["trend"] != entry_trend:
                _close_position(db, strategy, broker, open_position, "SUPERTREND_FLIP", now_price, symbol)
        return  # a position is open — never also evaluate a fresh entry this tick

    # 2. ENTRY — PAUSED strategies never take new entries (mirrors
    #    custom_strategy_scheduler.py's own PAUSED contract exactly).
    if strategy.status == "PAUSED":
        return
    if now_hhmm >= exit_time:
        return  # past today's cutoff — no new entries, whether or not anything traded today
    if _today_trade_count(db, strategy.id) >= max_trades:
        return

    try:
        signal = engine.evaluate_signal()
    except Exception as exc:
        log.error("intraday_indicator_scheduler: signal evaluation failed for strategy %s (%s): %s", strategy.id, strategy.name, exc)
        return
    if signal is None or signal["signal"] == "NONE":
        return

    option_type = "PE" if signal["signal"] == "BULLISH" else "CE"
    try:
        leg = engine.enter(option_type)
    except (ComplianceError, RuntimeError) as exc:
        log.error("intraday_indicator_scheduler: entry failed for strategy %s (%s): %s", strategy.id, strategy.name, exc)
        notify(
            "custom_strategy",
            f"Entry failed for strategy \"{strategy.name}\" ({mode} mode): {exc}. Will keep retrying while a signal is active.",
            level="warning", user_id=strategy.user_id,
        )
        return

    db.add(CustomStrategyPosition(
        strategy_id=strategy.id, leg_index=0, mode=_mode_for_status(strategy.status),
        instrument_key=leg["instrument_token"], instrument_type="OPTION",
        option_type=leg["option_type"], strike=leg["strike"], expiry=leg["expiry"],
        transaction_type="SELL", quantity=leg["quantity"], entry_price=leg["entry_price"] or 0,
        order_id=leg.get("order_id"), status="OPEN",
        leg_config_json=json.dumps({"entry_trend": signal["trend"], "entry_signal": signal["signal"]}),
    ))
    db.commit()

    details = f"SELL {leg['option_type']} {leg['strike']}@{leg['entry_price'] or 0:.2f} | qty={leg['quantity']} | signal={signal['signal']}"
    notify("custom_strategy", f"Trade Opened — {strategy.name}\n({mode})\n{details}", level="trade", user_id=strategy.user_id)
    alert_trade_opened(strategy.name, mode, symbol, details)
    log.info("intraday_indicator_scheduler: entered strategy %s (%s) — %s.", strategy.id, strategy.name, details)


async def intraday_indicator_scheduler() -> None:
    """Persistent background task — see module docstring. Never raises; logs and keeps ticking."""
    from automate.utils.single_instance_lock import acquire_singleton_lock

    if not acquire_singleton_lock("intraday_indicator_scheduler"):
        log.critical(
            "intraday_indicator_scheduler: another instance already holds this lock — refusing to start a "
            "second trading loop. If you're SURE no other instance of this app is actually running, delete "
            "logs/intraday_indicator_scheduler.lock and restart."
        )
        return

    log.info("intraday_indicator_scheduler: started.")
    while True:
        try:
            market_open = _market_is_open_now()
            if market_open:
                brokers = _get_brokers()
                if brokers is not None:
                    db = SessionLocal()
                    try:
                        strategies = db.query(CustomStrategy).filter(
                            CustomStrategy.strategy_type == "SUPERTREND_INTRADAY",
                            CustomStrategy.status.in_(["PAPER_TRADING", "LIVE", "PAUSED"]),
                        ).all()
                        for strategy in strategies:
                            _tick_one_strategy(db, strategy, brokers)
                    finally:
                        db.close()
        except Exception as exc:
            log.error("intraday_indicator_scheduler: tick-level failure: %s", exc, exc_info=True)

        await asyncio.sleep(_TICK_SEC_OPEN if market_open else _TICK_SEC_CLOSED)
