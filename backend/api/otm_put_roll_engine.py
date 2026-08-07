"""
api/otm_put_roll_engine.py — the ROLL-ADJUSTABLE engine for the far-month
OTM Nifty put-selling strategy (CustomStrategy rows with strategy_type ==
"OTM_PUT_ROLL", see strategies/custom/otm_put_roll_schema.py).

Provides ONLY _tick_one_strategy(db, strategy, brokers) — there is no
background loop/task here. Registered in api/strategy_scheduler.py's
dispatch table (the single shared scheduler loop every engine ticks
through) and in strategies/custom/engine_registry.py (route-layer
validate/describe/backtest-support) — adding this fourth engine needed
neither a new loop nor a new lock, just these two registrations, thanks
to the scheduler unification.

One SHORT PUT open at a time. Each tick, with a position open:
  1. Hard stop — exit_days_before_expiry, unconditionally (no overnight-
     into-settlement risk, same precedence every other engine gives this).
  2. Target — close everything if this cycle's CUMULATIVE P&L (every leg
     entered/rolled/closed so far this cycle, realized + the current
     open leg's unrealized) reaches target_capital_pct of the REAL
     broker margin captured at the cycle's first entry.
  3. Roll — if the latest CLOSED candle's close (never a raw tick — see
     otm_put_roll_schema.py's candle_interval_minutes) is below the
     current strike, close it and sell a new put roll_points lower, same
     expiry, UNLESS max_rolls_per_cycle has already been reached (then
     just hold and notify once — never rolls without bound).
With no position open: enter (SELL initial_otm_points below the
confirmed close, far-month expiry) once the confirmed close has pulled
back at least pullback_min_points off its pullback_lookback_days high,
and this expiry cycle hasn't already been entered.
"""
import json
from datetime import date, datetime

from compliance.sebi_rules import (
    AuditTrail,
    ComplianceError,
    OrderRateLimiter,
    get_global_kill_switch,
)
from db.models import CustomStrategy, CustomStrategyPosition
from strategies.custom.otm_put_roll_schema import get_setting
from strategies.custom.otm_put_roll_strategy import OtmPutRollStrategy
from utils.instrument_cache import InstrumentCache
from utils.logger import get_logger
from utils.notify import notify
from utils.option_utils import is_within_pre_expiry_buffer
from utils.telegram_alert import alert_trade_closed, alert_trade_opened

log = get_logger(__name__)

_audit = AuditTrail(audit_log_path="logs/otm_put_roll_audit.log")
_kill_switch = get_global_kill_switch()
_rate_limiter = OrderRateLimiter(max_per_second=10)


def _mode_for_status(status: str) -> str:
    return "paper" if status == "PAPER_TRADING" else "live"


def _get_cycle_marker(strategy: CustomStrategy) -> dict:
    if not strategy.last_entry_date:
        return {}
    try:
        data = json.loads(strategy.last_entry_date)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _set_cycle_marker(strategy: CustomStrategy, expiry: str, margin_at_entry: float | None) -> None:
    strategy.last_entry_date = json.dumps({"expiry": expiry, "date": date.today().isoformat(), "margin": margin_at_entry})


def _cycle_pnl(db, strategy_id: int, expiry: str, open_position: CustomStrategyPosition | None, open_now_price: float | None) -> float:
    """
    Cumulative realized + unrealized P&L (₹) across EVERY leg tied to this
    cycle (every roll shares the same `expiry`, so filtering on it scopes
    exactly "this cycle" even across several rolled-away legs). Every row
    this engine ever creates is a SELL (short put) — profit = entry
    premium collected minus what it cost to close (or its current price,
    for the still-open one).
    """
    positions = db.query(CustomStrategyPosition).filter(
        CustomStrategyPosition.strategy_id == strategy_id, CustomStrategyPosition.expiry == expiry,
    ).all()
    pnl = 0.0
    for p in positions:
        if p.status == "CLOSED" and p.exit_price is not None:
            pnl += (float(p.entry_price) - float(p.exit_price)) * p.quantity
        elif open_position is not None and p.id == open_position.id and open_now_price is not None:
            pnl += (float(p.entry_price) - open_now_price) * p.quantity
    return pnl


def _close_position(db, strategy: CustomStrategy, engine: OtmPutRollStrategy, position: CustomStrategyPosition, trigger: str) -> bool:
    """Square off the one open leg — atomic OPEN->CLOSING claim, same race-safety as every other engine's close helper. Reuses the engine instance _tick_one_strategy already built (close() doesn't touch self.rules, so it's safe regardless of which cycle's rules it was built from)."""
    from sqlalchemy import update

    claimed = db.execute(
        update(CustomStrategyPosition)
        .where(CustomStrategyPosition.id == position.id, CustomStrategyPosition.status == "OPEN")
        .values(status="CLOSING")
    ).rowcount
    db.commit()
    if claimed == 0:
        return False

    # The option contract itself may have expired and been dropped from
    # the instrument master since this leg was opened, so there's no live
    # price/order book to close against. Same fix as session_seller_engine.py
    # (see that module's _close_position for the full rationale): settle at
    # INTRINSIC value vs. the underlying's current spot (max(spot-strike,0)
    # for CE, max(strike-spot,0) for PE — never a fabricated ₹0, an ITM leg
    # still settles for real money) rather than leaving the leg stuck OPEN
    # and retried forever.
    if not InstrumentCache().instrument_exists(position.instrument_key):
        spot = engine.broker.get_ltp(engine.instrument_key)
        if spot is None:
            log.critical(
                "otm_put_roll_engine: leg %s for strategy %s expired/delisted and spot LTP for %s is also "
                "unavailable — cannot settle. MANUAL INTERVENTION REQUIRED.",
                position.instrument_key, strategy.id, engine.symbol,
            )
            notify(
                "custom_strategy",
                f"MANUAL INTERVENTION REQUIRED — \"{strategy.name}\" leg {position.instrument_key} "
                f"({position.transaction_type} {position.option_type} {position.strike}) has expired/delisted, "
                f"and this system could not fetch a spot price for {engine.symbol} to settle it either. Left OPEN "
                f"so this keeps retrying — please settle manually against your broker's contract note.",
                user_id=strategy.user_id,
            )
            position.status = "OPEN"
            db.commit()
            return False

        strike = float(position.strike)
        intrinsic = max(spot - strike, 0.0) if position.option_type == "CE" else max(strike - spot, 0.0)
        log.warning(
            "otm_put_roll_engine: leg %s for strategy %s expired/delisted — settling at intrinsic value %.2f "
            "(spot %s=%.2f, strike=%s %s).",
            position.instrument_key, strategy.id, intrinsic, engine.symbol, spot, position.strike, position.option_type,
        )
        position.status = "CLOSED"
        position.exit_price = intrinsic
        position.exit_reason = trigger
        position.closed_at = datetime.now()
        db.commit()
        notify(
            "custom_strategy",
            f"\"{strategy.name}\" leg {position.instrument_key} ({position.transaction_type} {position.option_type} "
            f"{position.strike}) had already expired/delisted by the time {trigger} ran — no live contract left to "
            f"close against. Settled at intrinsic value ₹{intrinsic:.2f} ({engine.symbol} spot was ₹{spot:.2f} vs "
            f"strike {position.strike}). Please cross-check against your broker's contract note if this was a live position.",
            level="warning", user_id=strategy.user_id,
        )
        return True

    try:
        result = engine.close(position.instrument_key, position.quantity, float(position.strike))
    except Exception as exc:
        log.critical("otm_put_roll_engine: FAILED to close leg %s for strategy %s: %s — MANUAL INTERVENTION REQUIRED.", position.instrument_key, strategy.id, exc)
        notify(
            "custom_strategy",
            f"MANUAL INTERVENTION REQUIRED — failed to close \"{strategy.name}\" leg {position.instrument_key} "
            f"({trigger} triggered): {exc}. A position may still be open on your real account.",
            user_id=strategy.user_id,
        )
        position.status = "OPEN"
        db.commit()
        return False

    position.status = "CLOSED"
    position.exit_price = result["exit_price"]
    position.exit_order_id = result["order_id"]
    position.exit_reason = trigger
    position.closed_at = datetime.now()
    db.commit()
    return True


def _tick_one_strategy(db, strategy: CustomStrategy, brokers: dict) -> None:
    """One strategy's worth of a tick. Never raises — logs and returns, same discipline as every other engine."""
    if not strategy.rules_json:
        return
    try:
        rules = json.loads(strategy.rules_json)
        symbols = json.loads(strategy.symbols)
        symbol = symbols[0]
    except (json.JSONDecodeError, IndexError):
        return

    open_position = db.query(CustomStrategyPosition).filter(
        CustomStrategyPosition.strategy_id == strategy.id, CustomStrategyPosition.status == "OPEN",
    ).first()

    broker_mode = (open_position.mode if open_position else None) if strategy.status == "PAUSED" else _mode_for_status(strategy.status)
    if broker_mode is None:
        return
    broker = brokers.get(broker_mode)
    if broker is None:
        return

    try:
        engine = OtmPutRollStrategy(broker=broker, audit=_audit, kill_switch=_kill_switch, rate_limiter=_rate_limiter, symbol=symbol, rules=rules, user_id=strategy.user_id)
    except Exception as exc:
        log.error("otm_put_roll_engine: could not build engine for strategy %s (%s): %s", strategy.id, strategy.name, exc)
        return

    mode_label = _mode_for_status(strategy.status)

    # 1-3. EXIT / ROLL — a position is open.
    if open_position is not None:
        expiry = open_position.expiry
        exit_days = get_setting(rules, "exit_days_before_expiry")
        try:
            expiry_date = date.fromisoformat(expiry)
        except (TypeError, ValueError):
            expiry_date = None

        if expiry_date is not None and is_within_pre_expiry_buffer(date.today(), expiry_date, exit_days):
            if _close_position(db, strategy, engine, open_position, "EXPIRY"):
                _log_close(strategy, mode_label, symbol, open_position, "EXPIRY")
            return

        now_price = broker.get_ltp(open_position.instrument_key)
        pnl = _cycle_pnl(db, strategy.id, expiry, open_position, now_price)

        margin_at_entry = _get_cycle_marker(strategy).get("margin")
        target_pct = get_setting(rules, "target_capital_pct")
        if margin_at_entry and pnl >= margin_at_entry * target_pct / 100.0:
            if _close_position(db, strategy, engine, open_position, "TAKE_PROFIT"):
                _log_close(strategy, mode_label, symbol, open_position, "TAKE_PROFIT")
            return

        try:
            confirmed_close = engine.confirmed_close()
        except Exception as exc:
            log.warning("otm_put_roll_engine: could not evaluate roll trigger for strategy %s (%s): %s", strategy.id, strategy.name, exc)
            return

        if confirmed_close < float(open_position.strike):
            max_rolls = get_setting(rules, "max_rolls_per_cycle")
            if open_position.leg_index >= max_rolls:
                already_notified = bool(json.loads(open_position.leg_config_json or "{}").get("max_rolls_notified"))
                if not already_notified:
                    notify(
                        "custom_strategy",
                        f"\"{strategy.name}\" has hit its {max_rolls}-roll cap and spot is still below the current "
                        f"strike ({open_position.strike}) — holding as-is rather than rolling further. Review manually; "
                        f"consider the futures-hedge fallback for a genuine tail move.",
                        level="warning", user_id=strategy.user_id,
                    )
                    open_position.leg_config_json = json.dumps({"max_rolls_notified": True})
                    db.commit()
                return
            if not _close_position(db, strategy, engine, open_position, "ROLL"):
                return
            try:
                new_leg = engine.roll(float(open_position.strike), expiry)
            except (ComplianceError, RuntimeError) as exc:
                log.error("otm_put_roll_engine: roll failed for strategy %s (%s): %s", strategy.id, strategy.name, exc)
                notify("custom_strategy", f"Roll failed for \"{strategy.name}\" ({mode_label} mode): {exc}. The old leg was already closed — this strategy is now FLAT until the next tick retries.", level="warning", user_id=strategy.user_id)
                return
            db.add(CustomStrategyPosition(
                strategy_id=strategy.id, leg_index=open_position.leg_index + 1, mode=mode_label,
                instrument_key=new_leg["instrument_token"], instrument_type="OPTION", option_type="PE",
                strike=new_leg["strike"], expiry=new_leg["expiry"], transaction_type="SELL",
                quantity=new_leg["quantity"], entry_price=new_leg["entry_price"] or 0,
                order_id=new_leg.get("order_id"), status="OPEN",
            ))
            db.commit()
            details = f"rolled {open_position.strike} -> {new_leg['strike']} | qty={new_leg['quantity']}@{new_leg['entry_price'] or 0:.2f}"
            notify("custom_strategy", f"Trade Rolled — {strategy.name}\n({mode_label})\n{details}", level="trade", user_id=strategy.user_id)
            alert_trade_opened(strategy.name, mode_label, symbol, details)
            log.info("otm_put_roll_engine: rolled strategy %s (%s) — %s.", strategy.id, strategy.name, details)
        return

    # ENTRY — no open position.
    if strategy.status == "PAUSED":
        return

    try:
        current_expiry = engine.resolve_far_month_expiry()
    except Exception as exc:
        log.warning("otm_put_roll_engine: could not resolve far-month expiry for strategy %s (%s): %s", strategy.id, strategy.name, exc)
        return
    if _get_cycle_marker(strategy).get("expiry") == current_expiry:
        return  # already traded this cycle

    try:
        pullback, confirmed_close = engine.pullback_points()
    except Exception as exc:
        log.warning("otm_put_roll_engine: could not evaluate pullback for strategy %s (%s): %s", strategy.id, strategy.name, exc)
        return
    if pullback < get_setting(rules, "pullback_min_points"):
        return

    try:
        leg = engine.enter(current_expiry, confirmed_close)
    except (ComplianceError, RuntimeError) as exc:
        log.error("otm_put_roll_engine: entry failed for strategy %s (%s): %s", strategy.id, strategy.name, exc)
        notify("custom_strategy", f"Entry failed for strategy \"{strategy.name}\" ({mode_label} mode): {exc}. Will keep retrying while the pullback condition holds.", level="warning", user_id=strategy.user_id)
        return

    margin_at_entry = None
    try:
        margin_at_entry = broker.get_basket_required_margin([{"instrument_key": leg["instrument_token"], "quantity": leg["quantity"], "transaction_type": "SELL", "product": "D"}])
    except Exception as exc:
        log.warning("otm_put_roll_engine: basket margin lookup failed for strategy %s (%s): %s", strategy.id, strategy.name, exc)

    _set_cycle_marker(strategy, current_expiry, margin_at_entry)
    db.add(CustomStrategyPosition(
        strategy_id=strategy.id, leg_index=0, mode=mode_label,
        instrument_key=leg["instrument_token"], instrument_type="OPTION", option_type="PE",
        strike=leg["strike"], expiry=leg["expiry"], transaction_type="SELL",
        quantity=leg["quantity"], entry_price=leg["entry_price"] or 0,
        order_id=leg.get("order_id"), status="OPEN",
    ))
    db.commit()

    details = f"SELL PE {leg['strike']}@{leg['entry_price'] or 0:.2f} | qty={leg['quantity']} | pullback={pullback:.0f}pts"
    notify("custom_strategy", f"Trade Opened — {strategy.name}\n({mode_label})\n{details}", level="trade", user_id=strategy.user_id)
    alert_trade_opened(strategy.name, mode_label, symbol, details)
    log.info("otm_put_roll_engine: entered strategy %s (%s) — %s.", strategy.id, strategy.name, details)


def _log_close(strategy: CustomStrategy, mode_label: str, symbol: str, position: CustomStrategyPosition, trigger: str) -> None:
    details = f"reason={trigger} | SELL PE {position.strike}@{position.exit_price or 0:.2f}"
    notify("custom_strategy", f"Trade Closed — {strategy.name}\n({mode_label})\n{details}", level="trade", user_id=strategy.user_id)
    alert_trade_closed(strategy.name, mode_label, symbol, details)
    log.info("otm_put_roll_engine: closed strategy %s (%s) | trigger=%s", strategy.id, strategy.name, trigger)
