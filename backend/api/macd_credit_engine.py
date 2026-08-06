"""
api/macd_credit_engine.py — the STOP-AND-REVERSE engine for the MACD-
based overnight directional credit-spread strategy (CustomStrategy rows
with strategy_type == "MACD_CREDIT_SPREAD", see strategies/custom/
macd_credit_schema.py).

Provides ONLY _tick_one_strategy(db, strategy, brokers) — no background
loop/task here, same as every other genuinely-new engine in this app.
Registered in api/strategy_scheduler.py's dispatch table and in
strategies/custom/engine_registry.py.

ALWAYS one 2-leg credit spread open (once started) — a Bull Put Spread
while the hourly MACD is bullish, a Bear Call Spread while bearish. Each
tick, with a position open:
  1. Hard stop — exit_days_before_expiry. Closes WITHOUT reversing (the
     system waits for the next real signal on the newly-rolled expiry
     instead of re-entering the same about-to-expire contract) — a
     marker records this so the very next tick doesn't just re-resolve
     the same still-listed expiry and immediately undo the safety close.
  2. Reversal — if the current hourly MACD trend no longer matches the
     open spread's own direction (short leg's option_type: PE = was
     BULLISH, CE = was BEARISH — read straight off the open position,
     no separate state needed), close the current spread and IMMEDIATELY
     open the opposite one, same tick — this system is never flat by
     design.
With no position open (only true at first start, or right after the
expiry-rollover safety close above): read the trend and open the
matching spread, once the block above has cleared.
"""
import json
from datetime import date, datetime

from compliance.sebi_rules import AuditTrail, ComplianceError, KillSwitch, OrderRateLimiter
from db.models import CustomStrategy, CustomStrategyPosition
from strategies.custom.macd_credit_schema import get_setting
from strategies.custom.macd_credit_strategy import MacdCreditStrategy
from utils.logger import get_logger
from utils.notify import notify
from utils.option_utils import is_within_pre_expiry_buffer
from utils.telegram_alert import alert_trade_closed, alert_trade_opened

log = get_logger(__name__)

_audit = AuditTrail(audit_log_path="logs/macd_credit_audit.log")
_kill_switch = KillSwitch()
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


def _close_position(db, strategy: CustomStrategy, engine: MacdCreditStrategy, position: CustomStrategyPosition, trigger: str) -> bool:
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
        result = engine.close_leg(position.instrument_key, position.quantity, float(position.strike), position.option_type, position.transaction_type)
    except Exception as exc:
        log.critical("macd_credit_engine: FAILED to close leg %s for strategy %s: %s — MANUAL INTERVENTION REQUIRED.", position.instrument_key, strategy.id, exc)
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


def _close_spread(db, strategy: CustomStrategy, engine: MacdCreditStrategy, positions: list[CustomStrategyPosition], trigger: str, mode_label: str, symbol: str) -> bool:
    closed_any = False
    for position in positions:
        if _close_position(db, strategy, engine, position, trigger):
            closed_any = True
    if closed_any:
        details = f"reason={trigger} | {len(positions)} legs squared off"
        notify("custom_strategy", f"Trade Closed — {strategy.name}\n({mode_label})\n{details}", level="trade", user_id=strategy.user_id)
        alert_trade_closed(strategy.name, mode_label, symbol, details)
        log.info("macd_credit_engine: closed spread for strategy %s (%s) | trigger=%s", strategy.id, strategy.name, trigger)
    return closed_any


def _enter_spread(db, strategy: CustomStrategy, engine: MacdCreditStrategy, expiry: str, trend: str, mode_label: str, symbol: str) -> bool:
    try:
        short_leg, hedge_leg = engine.enter(expiry, trend)
    except (ComplianceError, RuntimeError) as exc:
        log.error("macd_credit_engine: entry failed for strategy %s (%s): %s", strategy.id, strategy.name, exc)
        notify("custom_strategy", f"Entry failed for \"{strategy.name}\" ({mode_label} mode): {exc}. Will keep retrying every tick while this signal holds.", level="warning", user_id=strategy.user_id)
        return False

    for i, leg in enumerate((short_leg, hedge_leg)):
        db.add(CustomStrategyPosition(
            strategy_id=strategy.id, leg_index=i, mode=mode_label,
            instrument_key=leg["instrument_token"], instrument_type="OPTION", option_type=leg["option_type"],
            strike=leg["strike"], expiry=leg["expiry"], transaction_type=leg["transaction_type"],
            quantity=leg["quantity"], entry_price=leg["entry_price"] or 0, order_id=leg.get("order_id"), status="OPEN",
            leg_config_json=json.dumps({"role": leg["role"], "trend": trend}),
        ))
    _set_marker(strategy, blocked_until_expiry_rolls=None)
    db.commit()

    spread_label = "Bull Put Spread" if trend == "BULLISH" else "Bear Call Spread"
    details = f"{spread_label} — SHORT {short_leg['option_type']} {short_leg['strike']}@{short_leg['entry_price'] or 0:.2f} / HEDGE {hedge_leg['strike']}@{hedge_leg['entry_price'] or 0:.2f}"
    notify("custom_strategy", f"Trade Opened — {strategy.name}\n({mode_label})\n{details}", level="trade", user_id=strategy.user_id)
    alert_trade_opened(strategy.name, mode_label, symbol, details)
    log.info("macd_credit_engine: entered strategy %s (%s) — %s.", strategy.id, strategy.name, details)
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
        engine = MacdCreditStrategy(broker=broker, audit=_audit, kill_switch=_kill_switch, rate_limiter=_rate_limiter, symbol=symbol, rules=rules, user_id=strategy.user_id)
    except Exception as exc:
        log.error("macd_credit_engine: could not build engine for strategy %s (%s): %s", strategy.id, strategy.name, exc)
        return

    mode_label = _mode_for_status(strategy.status)

    # 1-2. EXIT / REVERSE — a spread is open.
    if open_positions:
        expiry = open_positions[0].expiry
        exit_days = get_setting(rules, "exit_days_before_expiry")
        try:
            expiry_date = date.fromisoformat(expiry)
        except (TypeError, ValueError):
            expiry_date = None

        if expiry_date is not None and is_within_pre_expiry_buffer(date.today(), expiry_date, exit_days):
            _close_spread(db, strategy, engine, open_positions, "EXPIRY", mode_label, symbol)
            _set_marker(strategy, blocked_until_expiry_rolls=expiry)
            db.commit()
            return

        short_leg = next((p for p in open_positions if _leg_meta(p).get("role") == "SHORT"), None)
        if short_leg is None:
            return  # mid-reversal (hedge closed, short not yet) — resume next tick
        current_trend = "BULLISH" if short_leg.option_type == "PE" else "BEARISH"

        try:
            live_trend = engine.read_trend()
        except Exception as exc:
            log.warning("macd_credit_engine: could not read MACD trend for strategy %s (%s): %s", strategy.id, strategy.name, exc)
            return
        if live_trend is None or live_trend == current_trend:
            return  # not enough history yet, or trend hasn't flipped — hold

        if not _close_spread(db, strategy, engine, open_positions, "REVERSAL", mode_label, symbol):
            return  # a close failed (already logged/notified inside) — retry next tick before reversing
        try:
            new_expiry = engine.resolve_expiry()
        except Exception as exc:
            log.warning("macd_credit_engine: could not resolve expiry for strategy %s (%s): %s", strategy.id, strategy.name, exc)
            return
        _enter_spread(db, strategy, engine, new_expiry, live_trend, mode_label, symbol)
        return

    # ENTRY — no open position (first start, or just past an expiry-rollover safety close).
    if strategy.status == "PAUSED":
        return

    try:
        current_expiry = engine.resolve_expiry()
    except Exception as exc:
        log.warning("macd_credit_engine: could not resolve expiry for strategy %s (%s): %s", strategy.id, strategy.name, exc)
        return
    if _get_marker(strategy).get("blocked_until_expiry_rolls") == current_expiry:
        return  # the expiry we just safety-closed hasn't actually rolled over to a new one yet

    try:
        trend = engine.read_trend()
    except Exception as exc:
        log.warning("macd_credit_engine: could not read MACD trend for strategy %s (%s): %s", strategy.id, strategy.name, exc)
        return
    if trend is None:
        return  # not enough hourly history yet

    _enter_spread(db, strategy, engine, current_expiry, trend, mode_label, symbol)
