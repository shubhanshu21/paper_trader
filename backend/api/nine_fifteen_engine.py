"""
api/nine_fifteen_engine.py — the SIGNAL-DRIVEN engine for the "9:15
Opening Range Breakout" stock-options-buying strategy (CustomStrategy
rows with strategy_type == "NINE_FIFTEEN_ORB", see strategies/custom/
nine_fifteen_schema.py).

Provides ONLY _tick_one_strategy(db, strategy, brokers) — no background
loop/task here, same as every other signal-driven engine. Registered in
api/strategy_scheduler.py's dispatch table and in strategies/custom/
engine_registry.py.

Two INDEPENDENT sides run every day, tracked separately in a per-day
marker (strategy.last_entry_date, same JSON-scratch-column pattern
zero_to_hero_engine.py uses):
  - GAINER side: today's #1 F&O stock gainer, watched for a break back
    ABOVE its own opening price -> BUY ATM CALL.
  - LOSER side: today's #1 F&O stock loser, watched for a break back
    BELOW its own opening price -> BUY ATM PUT.
Each side is scanned once (at scan_time), then watched independently
until it either breaks (enters) or entry_cutoff_time passes (skipped for
the day) — a side never blocks or depends on the other. Every open
position, on either side, is squared off unconditionally at exit_time —
this strategy has no target/stop-loss beyond that (see the schema's own
docstring: the short holding period itself IS the risk control here,
faithfully matching the reference strategy, not an oversight).
"""
import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

from compliance.sebi_rules import (
    AuditTrail,
    ComplianceError,
    OrderRateLimiter,
    get_global_kill_switch,
)
from db.models import CustomStrategy, CustomStrategyPosition
from strategies.custom.nine_fifteen_schema import get_setting
from strategies.custom.nine_fifteen_strategy import NineFifteenStrategy
from utils.logger import get_logger
from utils.notify import notify
from utils.telegram_alert import alert_trade_closed, alert_trade_opened

log = get_logger(__name__)

_IST = ZoneInfo("Asia/Kolkata")

_audit = AuditTrail(audit_log_path="logs/nine_fifteen_audit.log")
_kill_switch = get_global_kill_switch()
_rate_limiter = OrderRateLimiter(max_per_second=10)

_SIDES = {"GAINER": "CE", "LOSER": "PE"}


def _mode_for_status(status: str) -> str:
    return "paper" if status == "PAPER_TRADING" else "live"


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


def _today_marker(strategy: CustomStrategy) -> dict:
    """Today's marker, freshly reset if the stored one is from a prior day. `sides` holds per-side {"candidate": {...}|None, "done": bool}."""
    today_str = date.today().isoformat()
    marker = _get_marker(strategy)
    if marker.get("date") != today_str:
        return {"date": today_str, "scanned": False, "sides": {side: {"candidate": None, "done": False} for side in _SIDES}}
    return marker


def _close_leg(db, strategy: CustomStrategy, broker, position: CustomStrategyPosition, trigger: str, now_price: float | None, symbol: str) -> bool:
    """Square off ONE open BUY leg — same atomic OPEN->CLOSING claim + SELL-to-close pattern as zero_to_hero_engine.py's own _close_leg."""
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
        exit_order_id = broker.place_sell_order(
            instrument_token=position.instrument_key, quantity=position.quantity, product="MIS",
            order_type="MARKET", tag=f"915ORB_EXIT_{strategy.id}"[:20], user_id=strategy.user_id,
        )
    except Exception as exc:
        log.critical(
            "nine_fifteen_engine: FAILED to square off position %s for strategy %s: %s — MANUAL INTERVENTION REQUIRED.",
            position.instrument_key, strategy.id, exc,
        )
        notify(
            "custom_strategy",
            f"MANUAL INTERVENTION REQUIRED — failed to exit \"{strategy.name}\" position {position.instrument_key} "
            f"({trigger} triggered): {exc}. A position may still be open on your real account.",
            user_id=strategy.user_id,
        )
        position.status = "OPEN"
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
    """One strategy's worth of a tick. Never raises — logs and returns, same discipline every other engine in this app follows so one strategy's failure never stops the rest."""
    if not strategy.rules_json:
        return
    try:
        rules = json.loads(strategy.rules_json)
    except json.JSONDecodeError:
        return

    scan_time = get_setting(rules, "scan_time")
    observation_seconds = get_setting(rules, "observation_seconds")
    entry_cutoff = get_setting(rules, "entry_cutoff_time")
    exit_time = get_setting(rules, "exit_time")
    min_pct_move = get_setting(rules, "min_pct_move")
    lots = rules.get("lots", 1)

    open_positions = db.query(CustomStrategyPosition).filter(
        CustomStrategyPosition.strategy_id == strategy.id,
        CustomStrategyPosition.status == "OPEN",
    ).all()

    broker_mode = open_positions[0].mode if (strategy.status == "PAUSED" and open_positions) else _mode_for_status(strategy.status)
    broker = brokers.get(broker_mode)
    if broker is None:
        return

    now_dt = datetime.now(_IST)
    now_hhmm = now_dt.strftime("%H:%M")
    marker = _today_marker(strategy)

    # 1. EXIT — unconditional square-off at exit_time, every open leg on
    #    either side. No target/stop-loss otherwise (see module docstring).
    if open_positions:
        if now_hhmm >= exit_time:
            for position in open_positions:
                now_price = broker.get_ltp(position.instrument_key)
                try:
                    cfg = json.loads(position.leg_config_json or "{}")
                except json.JSONDecodeError:
                    cfg = {}
                _close_leg(db, strategy, broker, position, "TIME_EXIT", now_price, cfg.get("symbol", "?"))
        return  # a position is open — never also evaluate a fresh entry this tick

    # 2. ENTRY — PAUSED strategies never take new entries.
    if strategy.status == "PAUSED":
        return
    if now_hhmm >= exit_time:
        return  # past today's square-off cutoff

    try:
        engine = NineFifteenStrategy(
            broker=broker, audit=_audit, kill_switch=_kill_switch, rate_limiter=_rate_limiter,
            rules=rules, user_id=strategy.user_id,
        )
    except Exception as exc:
        log.error("nine_fifteen_engine: could not build engine for strategy %s (%s): %s", strategy.id, strategy.name, exc)
        return

    # One-time daily scan, once scan_time has arrived.
    if not marker["scanned"]:
        if now_hhmm < scan_time:
            return
        try:
            scan = engine.scan_top_movers()
        except Exception as exc:
            log.error("nine_fifteen_engine: scan failed for strategy %s (%s): %s", strategy.id, strategy.name, exc)
            return
        marker["scanned"] = True
        if scan is not None:
            for side, key in (("GAINER", "top_gainer"), ("LOSER", "top_loser")):
                candidate = scan[key]
                if min_pct_move and abs(candidate["pct_change"]) < min_pct_move:
                    marker["sides"][side]["done"] = True  # doesn't clear the bar — skip this side for today
                    continue
                marker["sides"][side]["candidate"] = candidate
        else:
            for side in _SIDES:
                marker["sides"][side]["done"] = True
        _set_marker(strategy, **marker)
        db.commit()
        if scan is None:
            return

    observation_elapsed = (now_dt.hour * 3600 + now_dt.minute * 60 + now_dt.second) >= (
        int(scan_time[:2]) * 3600 + int(scan_time[3:]) * 60 + observation_seconds
    )
    if not observation_elapsed:
        return

    for side, option_type in _SIDES.items():
        state = marker["sides"][side]
        if state["done"] or state["candidate"] is None:
            continue
        if now_hhmm >= entry_cutoff:
            state["done"] = True
            continue

        candidate = state["candidate"]
        symbol, instrument_key, opening_price = candidate["symbol"], candidate["instrument_key"], candidate["today_open"]
        if not opening_price:
            state["done"] = True
            continue

        ltp = broker.get_ltp(instrument_key)
        if ltp is None:
            continue  # try again next tick, still within the window
        breached = (ltp > opening_price) if side == "GAINER" else (ltp < opening_price)
        if not breached:
            continue

        quantity = lots * (broker.get_lot_size(symbol) or 0)
        if quantity <= 0:
            log.error("nine_fifteen_engine: could not resolve a real lot size for '%s' — skipping %s side.", symbol, side)
            state["done"] = True
            continue

        try:
            leg = engine.enter(symbol, instrument_key, option_type, quantity)
        except (ComplianceError, RuntimeError) as exc:
            log.error("nine_fifteen_engine: %s-side entry failed for strategy %s (%s): %s", side, strategy.id, strategy.name, exc)
            notify(
                "custom_strategy",
                f"{side.title()}-side entry failed for strategy \"{strategy.name}\" ({_mode_for_status(strategy.status)} mode): {exc}.",
                level="warning", user_id=strategy.user_id,
            )
            state["done"] = True
            continue

        db.add(CustomStrategyPosition(
            strategy_id=strategy.id, leg_index=0 if side == "GAINER" else 1, mode=_mode_for_status(strategy.status),
            instrument_key=leg["instrument_token"], instrument_type="OPTION",
            option_type=leg["option_type"], strike=leg["strike"], expiry=leg["expiry"],
            transaction_type="BUY", quantity=quantity, entry_price=leg["entry_price"] or 0,
            order_id=leg.get("order_id"), status="OPEN",
            leg_config_json=json.dumps({"side": side, "symbol": symbol, "opening_price": opening_price}),
        ))
        state["done"] = True
        mode = _mode_for_status(strategy.status)
        details = f"BUY {leg['option_type']} {symbol} {leg['strike']}@{leg['entry_price'] or 0:.2f} | qty={quantity} | {side} (pct_change={candidate['pct_change']:.2f}%)"
        notify("custom_strategy", f"Trade Opened — {strategy.name}\n({mode})\n{details}", level="trade", user_id=strategy.user_id)
        alert_trade_opened(strategy.name, mode, symbol, details)
        log.info("nine_fifteen_engine: entered strategy %s (%s) — %s.", strategy.id, strategy.name, details)

    _set_marker(strategy, **marker)
    db.commit()
