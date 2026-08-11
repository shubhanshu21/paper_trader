"""
api/weekly_directional_engine.py — the SIGNAL-DRIVEN engine for the
"Weekly Directional" asymmetric Reverse-Iron-Fly-with-tail-hedges
strategy (CustomStrategy rows with strategy_type ==
"WEEKLY_DIRECTIONAL", see strategies/custom/weekly_directional_schema.py).

Provides ONLY _tick_one_strategy(db, strategy, brokers) — no background
loop/task here, same as api/gravity_engine.py and
api/smart_condor_engine.py. Registered in api/strategy_scheduler.py's
dispatch table and in strategies/custom/engine_registry.py.

5 legs open at once (STRADDLE x2, SHORT x2, TAIL_HEDGE x1) — no mid-cycle
adjustment (the video describes none), just entry, then exit on capital-
based TP/SL or the expiry-buffer hard stop.
"""
import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

from compliance.sebi_rules import (
    AuditTrail,
    ComplianceError,
    get_global_kill_switch,
    get_rate_limiter_for,
)
from db.models import CustomStrategy, CustomStrategyPosition
from strategies.custom.weekly_directional_schema import get_setting
from strategies.custom.weekly_directional_strategy import WeeklyDirectionalStrategy
from utils.instrument_cache import InstrumentCache
from utils.logger import get_logger
from utils.notify import notify
from utils.option_utils import is_within_pre_expiry_buffer
from utils.telegram_alert import alert_trade_closed, alert_trade_opened

log = get_logger(__name__)

_IST = ZoneInfo("Asia/Kolkata")

_audit = AuditTrail(audit_log_path="logs/weekly_directional_audit.log")
_kill_switch = get_global_kill_switch()

_WEEKDAY_NUMS = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}


def _mode_for_status(status: str) -> str:
    return "paper" if status == "PAPER_TRADING" else "live"


def _leg_meta(position: CustomStrategyPosition) -> dict:
    try:
        return json.loads(position.leg_config_json or "{}")
    except json.JSONDecodeError:
        return {}


def _get_cycle_marker(strategy: CustomStrategy) -> dict:
    if not strategy.last_entry_date:
        return {}
    try:
        data = json.loads(strategy.last_entry_date)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _set_cycle_marker(strategy: CustomStrategy, **updates) -> None:
    marker = _get_cycle_marker(strategy)
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


def _close_position(db, strategy: CustomStrategy, engine: WeeklyDirectionalStrategy, position: CustomStrategyPosition, trigger: str) -> bool:
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
                "weekly_directional_engine: leg %s for strategy %s expired/delisted and spot LTP for %s is also "
                "unavailable — cannot settle. MANUAL INTERVENTION REQUIRED.",
                position.instrument_key, strategy.id, engine.symbol,
            )
            notify(
                "custom_strategy",
                f"MANUAL INTERVENTION REQUIRED — \"{strategy.name}\" leg {position.instrument_key} "
                f"({position.transaction_type} {position.option_type} {position.strike}) has expired/delisted, "
                f"and this system could not fetch a spot price for {engine.symbol} to settle it either. Left OPEN "
                "so this keeps retrying — please settle manually against your broker's contract note.",
                user_id=strategy.user_id,
            )
            position.status = "OPEN"
            db.commit()
            return False

        strike = float(position.strike)
        intrinsic = max(spot - strike, 0.0) if position.option_type == "CE" else max(strike - spot, 0.0)
        log.warning(
            "weekly_directional_engine: leg %s for strategy %s expired/delisted — settling at intrinsic value %.2f "
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
        result = engine.close_leg(position.instrument_key, position.quantity, float(position.strike), position.option_type, position.transaction_type)
    except Exception as exc:
        log.critical("weekly_directional_engine: FAILED to close leg %s for strategy %s: %s — MANUAL INTERVENTION REQUIRED.", position.instrument_key, strategy.id, exc)
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


def _close_all(db, strategy: CustomStrategy, engine: WeeklyDirectionalStrategy, positions: list[CustomStrategyPosition], trigger: str, mode_label: str, symbol: str) -> None:
    closed_any = False
    for position in positions:
        if _close_position(db, strategy, engine, position, trigger):
            closed_any = True
    if closed_any:
        details = f"reason={trigger} | {len(positions)} legs squared off"
        notify("custom_strategy", f"Trade Closed — {strategy.name}\n({mode_label})\n{details}", level="trade", user_id=strategy.user_id)
        alert_trade_closed(strategy.name, mode_label, symbol, details)
        log.info("weekly_directional_engine: closed all legs for strategy %s (%s) | trigger=%s", strategy.id, strategy.name, trigger)


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
        engine = WeeklyDirectionalStrategy(broker=broker, audit=_audit, kill_switch=_kill_switch, rate_limiter=get_rate_limiter_for(strategy.user_id), symbol=symbol, rules=rules, user_id=strategy.user_id)
    except Exception as exc:
        log.error("weekly_directional_engine: could not build engine for strategy %s (%s): %s", strategy.id, strategy.name, exc)
        return

    mode_label = _mode_for_status(strategy.status)
    now = datetime.now(_IST)

    # EXIT — a position is open.
    if open_positions:
        expiry = open_positions[0].expiry
        exit_days = get_setting(rules, "exit_days_before_expiry")
        try:
            expiry_date = date.fromisoformat(expiry)
        except (TypeError, ValueError):
            expiry_date = None
        if expiry_date is not None and is_within_pre_expiry_buffer(datetime.now(_IST).date(), expiry_date, exit_days):
            _close_all(db, strategy, engine, open_positions, "EXPIRY", mode_label, symbol)
            return

        margin_at_entry = _get_cycle_marker(strategy).get("margin")
        if margin_at_entry:
            pnl = _cycle_pnl(db, strategy.id, expiry, broker)
            target_pct = get_setting(rules, "target_capital_pct")
            stop_pct = get_setting(rules, "stop_loss_capital_pct")
            if pnl >= margin_at_entry * target_pct / 100.0:
                _close_all(db, strategy, engine, open_positions, "TAKE_PROFIT", mode_label, symbol)
                return
            if pnl <= -margin_at_entry * stop_pct / 100.0:
                _close_all(db, strategy, engine, open_positions, "STOP_LOSS", mode_label, symbol)
                return
        return

    # ENTRY — no open position.
    if strategy.status == "PAUSED":
        return

    try:
        current_expiry = engine.resolve_expiry()
    except Exception as exc:
        log.warning("weekly_directional_engine: could not resolve weekly expiry for strategy %s (%s): %s", strategy.id, strategy.name, exc)
        return
    if _get_cycle_marker(strategy).get("expiry") == current_expiry:
        return  # already traded (and, presumably, exited) this cycle

    entry_weekday = get_setting(rules, "entry_weekday")
    entry_time = get_setting(rules, "entry_time")
    if _WEEKDAY_NUMS.get(entry_weekday) != now.weekday():
        return
    if now.strftime("%H:%M") < entry_time:
        return

    try:
        direction = engine.determine_direction()
    except Exception as exc:
        log.warning("weekly_directional_engine: could not read EMA direction for strategy %s (%s): %s", strategy.id, strategy.name, exc)
        return
    if direction is None:
        # No clear bias today (EMAs equal or not enough history) — mark this
        # expiry as "considered" so we don't re-check every 30s for the rest
        # of the entry window, but don't enter; next week's a fresh check.
        _set_cycle_marker(strategy, expiry=current_expiry)
        db.commit()
        log.info("weekly_directional_engine: strategy %s (%s) has no clear EMA bias today — skipping this cycle.", strategy.id, strategy.name)
        return

    try:
        legs = engine.enter(current_expiry, direction)
    except (ComplianceError, RuntimeError) as exc:
        log.error("weekly_directional_engine: entry failed for strategy %s (%s): %s", strategy.id, strategy.name, exc)
        notify("custom_strategy", f"Entry failed for strategy \"{strategy.name}\" ({mode_label} mode): {exc}. Will keep retrying every tick until this entry window closes.", level="warning", user_id=strategy.user_id)
        return

    margin_at_entry = None
    try:
        basket = [{"instrument_key": leg["instrument_token"], "quantity": leg["quantity"], "transaction_type": leg["transaction_type"], "product": "D"} for leg in legs]
        margin_at_entry = broker.get_basket_required_margin(basket)
    except Exception as exc:
        log.warning("weekly_directional_engine: basket margin lookup failed for strategy %s (%s): %s", strategy.id, strategy.name, exc)

    _set_cycle_marker(strategy, expiry=current_expiry, direction=direction, margin=margin_at_entry)
    for i, leg in enumerate(legs):
        db.add(CustomStrategyPosition(
            strategy_id=strategy.id, leg_index=i, mode=mode_label,
            instrument_key=leg["instrument_token"], instrument_type="OPTION", option_type=leg["option_type"],
            strike=leg["strike"], expiry=leg["expiry"], transaction_type=leg["transaction_type"],
            quantity=leg["quantity"], entry_price=leg["entry_price"] or 0, order_id=leg.get("order_id"), status="OPEN",
            leg_config_json=json.dumps({"role": leg["role"]}),
        ))
    db.commit()

    details = f"{direction} bias — " + ", ".join(f"{leg['transaction_type']} {leg['option_type']} {leg['strike']}@{leg['entry_price'] or 0:.2f}" for leg in legs)
    notify("custom_strategy", f"Trade Opened — {strategy.name}\n({mode_label})\n{details}", level="trade", user_id=strategy.user_id)
    alert_trade_opened(strategy.name, mode_label, symbol, details)
    log.info("weekly_directional_engine: entered strategy %s (%s) — %s.", strategy.id, strategy.name, details)
