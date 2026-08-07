"""
api/gravity_engine.py — the SIGNAL-DRIVEN credit-spread engine for the
"Gravity" Camarilla fakeout-reversal strategy (CustomStrategy rows with
strategy_type == "GRAVITY", see strategies/custom/gravity_schema.py).

Provides ONLY _tick_one_strategy(db, strategy, brokers) — no background
loop/task here, same as api/otm_put_roll_engine.py and
api/smart_condor_engine.py. Registered in api/strategy_scheduler.py's
dispatch table and in strategies/custom/engine_registry.py.

One 2-leg credit spread (SOLD + HEDGE) open at a time. Each tick, with a
position open:
  1. Hard stop — exit_days_before_expiry, unconditionally.
  2. Price-level stop-loss — spot has reached (or crossed through) the
     sold strike (the sold leg has gone ATM/ITM) — a real price level,
     not a %-of-premium one, per gravity_schema.py.
  3. Target — cumulative P&L has captured target_credit_pct of the net
     credit collected at entry.
With no position open: evaluate the daily Camarilla breach+confirm signal
(only once signal_check_time has passed — today's daily candle needs to
be closed enough to trust) once per day, skip if today falls inside a
configured earnings blackout window or the spread's max ROI is below
min_roi_pct, otherwise enter.
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
from strategies.custom.gravity_schema import get_setting
from strategies.custom.gravity_strategy import GravityStrategy
from utils.instrument_cache import InstrumentCache
from utils.logger import get_logger
from utils.notify import notify
from utils.option_utils import is_within_pre_expiry_buffer
from utils.telegram_alert import alert_trade_closed, alert_trade_opened

log = get_logger(__name__)

_audit = AuditTrail(audit_log_path="logs/gravity_audit.log")
_kill_switch = get_global_kill_switch()
_rate_limiter = OrderRateLimiter(max_per_second=10)


def _mode_for_status(status: str) -> str:
    return "paper" if status == "PAPER_TRADING" else "live"


def _leg_meta(position: CustomStrategyPosition) -> dict:
    try:
        return json.loads(position.leg_config_json or "{}")
    except json.JSONDecodeError:
        return {}


def _get_marker(strategy: CustomStrategy) -> dict:
    if not strategy.last_entry_date:
        return {}
    try:
        data = json.loads(strategy.last_entry_date)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _set_marker(strategy: CustomStrategy, **updates) -> None:
    marker = _get_marker(strategy)
    marker.update(updates)
    strategy.last_entry_date = json.dumps(marker)


def _leg_pnl(position: CustomStrategyPosition, current_price: float | None) -> float:
    if position.status == "CLOSED" and position.exit_price is not None:
        price = float(position.exit_price)
    elif current_price is not None:
        price = current_price
    else:
        return 0.0
    entry = float(position.entry_price)
    diff = (entry - price) if position.transaction_type == "SELL" else (price - entry)
    return diff * position.quantity


def _cycle_pnl(db, strategy_id: int, expiry: str, broker) -> float:
    positions = db.query(CustomStrategyPosition).filter(
        CustomStrategyPosition.strategy_id == strategy_id, CustomStrategyPosition.expiry == expiry,
    ).all()
    total = 0.0
    for p in positions:
        if p.status == "CLOSED":
            total += _leg_pnl(p, None)
        else:
            total += _leg_pnl(p, broker.get_ltp(p.instrument_key))
    return total


def _close_position(db, strategy: CustomStrategy, engine: GravityStrategy, position: CustomStrategyPosition, trigger: str) -> bool:
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
                "gravity_engine: leg %s for strategy %s expired/delisted and spot LTP for %s is also "
                "unavailable — cannot settle. MANUAL INTERVENTION REQUIRED.",
                position.instrument_key, strategy.id, engine.symbol,
            )
            notify(
                "custom_strategy",
                "MANUAL INTERVENTION REQUIRED — \"{strategy.name}\" leg {position.instrument_key} "
                "({position.transaction_type} {position.option_type} {position.strike}) has expired/delisted, "
                "and this system could not fetch a spot price for {engine.symbol} to settle it either. Left OPEN "
                "so this keeps retrying — please settle manually against your broker's contract note.",
                user_id=strategy.user_id,
            )
            position.status = "OPEN"
            db.commit()
            return False

        strike = float(position.strike)
        intrinsic = max(spot - strike, 0.0) if position.option_type == "CE" else max(strike - spot, 0.0)
        log.warning(
            "gravity_engine: leg %s for strategy %s expired/delisted — settling at intrinsic value %.2f "
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
            "\"{strategy.name}\" leg {position.instrument_key} ({position.transaction_type} {position.option_type} "
            "{position.strike}) had already expired/delisted by the time {trigger} ran — no live contract left to "
            "close against. Settled at intrinsic value ₹{intrinsic:.2f} ({engine.symbol} spot was ₹{spot:.2f} vs "
            "strike {position.strike}). Please cross-check against your broker's contract note if this was a live position.",
            level="warning", user_id=strategy.user_id,
        )
        return True

    try:
        result = engine.close_leg(position.instrument_key, position.quantity, float(position.strike), position.option_type, position.transaction_type)
    except Exception as exc:
        log.critical("gravity_engine: FAILED to close leg %s for strategy %s: %s — MANUAL INTERVENTION REQUIRED.", position.instrument_key, strategy.id, exc)
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


def _close_all(db, strategy: CustomStrategy, engine: GravityStrategy, positions: list[CustomStrategyPosition], trigger: str, mode_label: str, symbol: str) -> None:
    closed_any = False
    for position in positions:
        if _close_position(db, strategy, engine, position, trigger):
            closed_any = True
    if closed_any:
        details = f"reason={trigger} | {len(positions)} legs squared off"
        notify("custom_strategy", f"Trade Closed — {strategy.name}\n({mode_label})\n{details}", level="trade", user_id=strategy.user_id)
        alert_trade_closed(strategy.name, mode_label, symbol, details)
        log.info("gravity_engine: closed all legs for strategy %s (%s) | trigger=%s", strategy.id, strategy.name, trigger)


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

    open_positions = db.query(CustomStrategyPosition).filter(
        CustomStrategyPosition.strategy_id == strategy.id, CustomStrategyPosition.status == "OPEN",
    ).all()

    broker_mode = (open_positions[0].mode if open_positions else None) if strategy.status == "PAUSED" else _mode_for_status(strategy.status)
    if broker_mode is None:
        return
    broker = brokers.get(broker_mode)
    if broker is None:
        return

    try:
        engine = GravityStrategy(broker=broker, audit=_audit, kill_switch=_kill_switch, rate_limiter=_rate_limiter, symbol=symbol, rules=rules, user_id=strategy.user_id)
    except Exception as exc:
        log.error("gravity_engine: could not build engine for strategy %s (%s): %s", strategy.id, strategy.name, exc)
        return

    mode_label = _mode_for_status(strategy.status)

    # 1-3. EXIT — a position is open.
    if open_positions:
        expiry = open_positions[0].expiry
        exit_days = get_setting(rules, "exit_days_before_expiry")
        try:
            expiry_date = date.fromisoformat(expiry)
        except (TypeError, ValueError):
            expiry_date = None
        if expiry_date is not None and is_within_pre_expiry_buffer(date.today(), expiry_date, exit_days):
            _close_all(db, strategy, engine, open_positions, "EXPIRY", mode_label, symbol)
            return

        sold_leg = next((p for p in open_positions if _leg_meta(p).get("role") == "SOLD"), None)
        if sold_leg is not None:
            spot = broker.get_ltp(engine.instrument_key)
            if spot is not None:
                signal = _get_marker(strategy).get("signal")
                stop_hit = (spot <= float(sold_leg.strike)) if signal == "BULLISH" else (spot >= float(sold_leg.strike))
                if stop_hit:
                    _close_all(db, strategy, engine, open_positions, "STOP_LOSS", mode_label, symbol)
                    return

        net_credit = _get_marker(strategy).get("net_credit")
        if net_credit:
            pnl = _cycle_pnl(db, strategy.id, expiry, broker)
            target_pct = get_setting(rules, "target_credit_pct")
            if pnl >= net_credit * target_pct / 100.0:
                _close_all(db, strategy, engine, open_positions, "TAKE_PROFIT", mode_label, symbol)
                return
        return

    # ENTRY — no open position.
    if strategy.status == "PAUSED":
        return

    today = date.today()
    if engine.is_blacked_out(today):
        return

    check_time = get_setting(rules, "signal_check_time")
    if datetime.now().strftime("%H:%M") < check_time:
        return

    if _get_marker(strategy).get("last_signal_date") == today.isoformat():
        return  # already evaluated (and either entered or passed on) today's signal

    try:
        signal_result = engine.evaluate_signal()
    except Exception as exc:
        log.warning("gravity_engine: could not evaluate signal for strategy %s (%s): %s", strategy.id, strategy.name, exc)
        return

    _set_marker(strategy, last_signal_date=today.isoformat())
    db.commit()
    if signal_result is None:
        return

    try:
        current_expiry = engine.resolve_expiry()
    except Exception as exc:
        log.warning("gravity_engine: could not resolve expiry for strategy %s (%s): %s", strategy.id, strategy.name, exc)
        return

    try:
        preview = engine.preview_spread(current_expiry, signal_result["signal"], signal_result["extreme"])
    except Exception as exc:
        log.warning("gravity_engine: could not preview spread for strategy %s (%s): %s", strategy.id, strategy.name, exc)
        return

    min_roi = get_setting(rules, "min_roi_pct")
    roi_pct = (preview["net_credit"] / preview["max_loss"] * 100.0) if preview["max_loss"] > 0 else 0.0
    if roi_pct < min_roi:
        log.info("gravity_engine: strategy %s (%s) signal fired but ROI %.1f%% < min_roi_pct %.1f%% — skipping.", strategy.id, strategy.name, roi_pct, min_roi)
        return

    try:
        sold_leg, hedge_leg = engine.enter(current_expiry, signal_result["signal"], signal_result["extreme"])
    except (ComplianceError, RuntimeError) as exc:
        log.error("gravity_engine: entry failed for strategy %s (%s): %s", strategy.id, strategy.name, exc)
        notify("custom_strategy", f"Entry failed for strategy \"{strategy.name}\" ({mode_label} mode): {exc}.", level="warning", user_id=strategy.user_id)
        return

    net_credit, _max_loss = engine.net_credit_and_max_loss(sold_leg, hedge_leg)
    _set_marker(strategy, expiry=current_expiry, signal=signal_result["signal"], net_credit=net_credit, last_signal_date=today.isoformat())
    for i, leg in enumerate((sold_leg, hedge_leg)):
        db.add(CustomStrategyPosition(
            strategy_id=strategy.id, leg_index=i, mode=mode_label,
            instrument_key=leg["instrument_token"], instrument_type="OPTION", option_type=leg["option_type"],
            strike=leg["strike"], expiry=leg["expiry"], transaction_type=leg["transaction_type"],
            quantity=leg["quantity"], entry_price=leg["entry_price"] or 0, order_id=leg.get("order_id"), status="OPEN",
            leg_config_json=json.dumps({"role": leg["role"]}),
        ))
    db.commit()

    details = f"{signal_result['signal']} fakeout — SOLD {sold_leg['option_type']} {sold_leg['strike']}@{sold_leg['entry_price'] or 0:.2f} / HEDGE {hedge_leg['strike']}@{hedge_leg['entry_price'] or 0:.2f} | net_credit={net_credit:.2f}"
    notify("custom_strategy", f"Trade Opened — {strategy.name}\n({mode_label})\n{details}", level="trade", user_id=strategy.user_id)
    alert_trade_opened(strategy.name, mode_label, symbol, details)
    log.info("gravity_engine: entered strategy %s (%s) — %s.", strategy.id, strategy.name, details)
