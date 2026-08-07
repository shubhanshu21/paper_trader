"""
api/session_seller_engine.py — the DUAL-SESSION INTRADAY engine for the
"Intraday Nifty & Sensex Option Selling" strategy (CustomStrategy rows
with strategy_type == "SESSION_SELLER", see strategies/custom/
session_seller_schema.py).

Provides ONLY _tick_one_strategy(db, strategy, brokers) — no background
loop/task here, same as every other genuinely-new engine this app has.
Registered in api/strategy_scheduler.py's dispatch table and in
strategies/custom/engine_registry.py.

One trading day, one active symbol (off the weekday schedule), up to 4
legs: 2 HEDGE (bought once, held through both sessions) + up to 2 SHORT
per session (morning strikes at ATM+-otm_points, afternoon strikes at
plain ATM, reusing the same hedges). Each tick:
  1. SAFETY — any OPEN leg tagged with a date OTHER than today gets force-
     closed immediately (should never happen if the afternoon exit ran
     cleanly the day before; a crash/restart mid-session is the only way
     it could) — this strategy NEVER holds anything overnight, no
     exceptions.
  2. Per-leg 50% stop-loss on any open SHORT leg, checked independently —
     a stopped-out leg is simply left closed; it is NOT replaced until
     the next scheduled entry (morning or afternoon), and the other leg
     keeps running untouched.
  3. Morning exit deadline — force-close any remaining OPEN morning SHORT
     legs (hedges stay open, reused by the afternoon session).
  4. Afternoon exit deadline — force-close EVERYTHING still open (shorts
     and hedges) — end of day, unconditionally.
  5. ENTRY — morning hedges+shorts once `morning_entry` is reached (once
     per day); afternoon shorts once `afternoon_entry` is reached AND the
     morning session actually happened (once per day) — skipped entirely
     if today isn't in the symbol schedule or falls inside a blackout
     window.
"""
import json
from datetime import date, datetime

from compliance.sebi_rules import AuditTrail, ComplianceError, KillSwitch, OrderRateLimiter
from db.models import CustomStrategy, CustomStrategyPosition
from strategies.custom.session_seller_schema import get_setting
from strategies.custom.session_seller_strategy import SessionSellerStrategy
from utils.instrument_cache import InstrumentCache
from utils.logger import get_logger
from utils.notify import notify
from utils.telegram_alert import alert_trade_closed, alert_trade_opened

log = get_logger(__name__)

_audit = AuditTrail(audit_log_path="logs/session_seller_audit.log")
_kill_switch = KillSwitch()
_rate_limiter = OrderRateLimiter(max_per_second=10)

_WEEKDAY_NAMES = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]


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


def _is_blacked_out(rules: dict, today: date) -> bool:
    for window in get_setting(rules, "blackout_dates") or []:
        if date.fromisoformat(window["start"]) <= today <= date.fromisoformat(window["end"]):
            return True
    return False


def _close_position(db, strategy: CustomStrategy, engine: SessionSellerStrategy, position: CustomStrategyPosition, trigger: str) -> bool:
    from sqlalchemy import update

    claimed = db.execute(
        update(CustomStrategyPosition)
        .where(CustomStrategyPosition.id == position.id, CustomStrategyPosition.status == "OPEN")
        .values(status="CLOSING")
    ).rowcount
    db.commit()
    if claimed == 0:
        return False

    # This session's contracts are single-day (see module docstring — never
    # held overnight), so by the time OVERNIGHT_SAFETY fires on the leg,
    # its option may have already expired and been dropped from the
    # instrument master entirely — there is then no live price and no order
    # book to place a closing order against. A real broker doesn't give up
    # here either: an expired index option cash-settles at INTRINSIC value
    # vs. the underlying's expiry close (max(spot-strike,0) for CE,
    # max(strike-spot,0) for PE — same formula as
    # utils.option_utils.strategy_payoff_at_expiry) — never a fabricated
    # ₹0, an ITM leg still settles for real money. Unlike the option, the
    # underlying INDEX itself never expires/delists, so its LTP is always
    # resolvable via engine.instrument_key. This is exact when caught
    # promptly (OVERNIGHT_SAFETY's normal case, next tick after the missed
    # same-day exit); if the process was down long enough to miss the
    # expiry-day close entirely, "current spot" is a later price than the
    # true settlement print, so the notification below says which spot was
    # used so it can be sanity-checked against the real contract note.
    if not InstrumentCache().instrument_exists(position.instrument_key):
        spot = engine.broker.get_ltp(engine.instrument_key)
        if spot is None:
            log.critical(
                "session_seller_engine: leg %s for strategy %s expired/delisted and spot LTP for %s is also "
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
            "session_seller_engine: leg %s for strategy %s expired/delisted — settling at intrinsic value %.2f "
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
            f"close against. Settled at intrinsic value ₹{intrinsic:.2f} (spot {engine.symbol}=₹{spot:.2f} vs strike "
            f"{position.strike}). Please cross-check against your broker's contract note if this was a live position.",
            level="warning", user_id=strategy.user_id,
        )
        return True

    try:
        result = engine.close_leg(position.instrument_key, position.quantity, float(position.strike), position.option_type, position.transaction_type)
    except Exception as exc:
        log.critical("session_seller_engine: FAILED to close leg %s for strategy %s: %s — MANUAL INTERVENTION REQUIRED.", position.instrument_key, strategy.id, exc)
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


def _close_many(db, strategy: CustomStrategy, engine: SessionSellerStrategy, positions: list[CustomStrategyPosition], trigger: str, mode_label: str, symbol: str) -> None:
    closed_any = False
    for position in positions:
        if _close_position(db, strategy, engine, position, trigger):
            closed_any = True
    if closed_any:
        details = f"reason={trigger} | {len(positions)} leg(s) squared off"
        notify("custom_strategy", f"Trade Closed — {strategy.name}\n({mode_label})\n{details}", level="trade", user_id=strategy.user_id)
        alert_trade_closed(strategy.name, mode_label, symbol, details)
        log.info("session_seller_engine: closed legs for strategy %s (%s) | trigger=%s", strategy.id, strategy.name, trigger)


def _build_engine(broker, symbol: str, rules: dict, user_id: int | None) -> SessionSellerStrategy | None:
    try:
        return SessionSellerStrategy(broker=broker, audit=_audit, kill_switch=_kill_switch, rate_limiter=_rate_limiter, symbol=symbol, rules=rules, user_id=user_id)
    except Exception:
        return None


def _tick_one_strategy(db, strategy: CustomStrategy, brokers: dict) -> None:
    """One strategy's worth of a tick. Never raises — logs and returns, same discipline as every other engine."""
    if not strategy.rules_json:
        return
    try:
        rules = json.loads(strategy.rules_json)
    except json.JSONDecodeError:
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

    mode_label = _mode_for_status(strategy.status)
    today = date.today()
    today_str = today.isoformat()
    now_hhmm = datetime.now().strftime("%H:%M")

    # 1. SAFETY — never carry anything overnight, regardless of what today's schedule says.
    stale = [p for p in open_positions if _leg_meta(p).get("date") != today_str]
    if stale:
        stale_symbol = _leg_meta(stale[0]).get("symbol", "")
        stale_engine = _build_engine(broker, stale_symbol, rules, strategy.user_id) if stale_symbol else None
        if stale_engine is not None:
            _close_many(db, strategy, stale_engine, stale, "OVERNIGHT_SAFETY", mode_label, stale_symbol)
        return  # don't also evaluate today's fresh state this same tick
    open_positions = [p for p in open_positions if p not in stale]

    marker = _get_marker(strategy)
    if marker.get("date") != today_str:
        marker = {"date": today_str}
        _set_marker(strategy, **marker)
        db.commit()

    # Prefer today's ALREADY-TRADED symbol (from open legs) over the schedule, so an
    # intraday schedule edit can never fork today's decisions mid-session.
    active_symbol = _leg_meta(open_positions[0]).get("symbol") if open_positions else get_setting(rules, "symbol_schedule").get(_WEEKDAY_NAMES[today.weekday()])
    if not active_symbol:
        return  # not a trading day for this strategy at all

    engine = _build_engine(broker, active_symbol, rules, strategy.user_id)
    if engine is None:
        log.error("session_seller_engine: could not build engine for strategy %s (%s) symbol %s.", strategy.id, strategy.name, active_symbol)
        return

    session_cfg = get_setting(rules, "sessions").get(active_symbol, {})
    stop_loss_pct = get_setting(rules, "stop_loss_pct")

    # 2. Per-leg 50% stop-loss — independent per SHORT leg, no re-entry this session.
    for leg in [p for p in open_positions if _leg_meta(p).get("role") == "SHORT"]:
        price = broker.get_ltp(leg.instrument_key)
        sl_hit = price is not None and price >= float(leg.entry_price) * (1 + stop_loss_pct / 100.0)
        if sl_hit and _close_position(db, strategy, engine, leg, "STOP_LOSS"):
            details = f"reason=STOP_LOSS | {leg.option_type} {leg.strike} ({_leg_meta(leg).get('session')}) closed at {stop_loss_pct}% loss — leg not re-entered this session"
            notify("custom_strategy", f"Trade Closed — {strategy.name}\n({mode_label})\n{details}", level="trade", user_id=strategy.user_id)
            alert_trade_closed(strategy.name, mode_label, active_symbol, details)

    # Re-read open positions after any SL closes above.
    open_positions = db.query(CustomStrategyPosition).filter(
        CustomStrategyPosition.strategy_id == strategy.id, CustomStrategyPosition.status == "OPEN",
    ).all()

    # 3. Morning exit deadline — shorts only, hedges stay open for the afternoon session.
    morning_shorts = [p for p in open_positions if _leg_meta(p).get("role") == "SHORT" and _leg_meta(p).get("session") == "MORNING"]
    if morning_shorts and now_hhmm >= session_cfg.get("morning_exit", "99:99"):
        _close_many(db, strategy, engine, morning_shorts, "TIME_EXIT", mode_label, active_symbol)

    # 4. Afternoon exit deadline — everything, unconditionally.
    if now_hhmm >= session_cfg.get("afternoon_exit", "99:99"):
        remaining = db.query(CustomStrategyPosition).filter(
            CustomStrategyPosition.strategy_id == strategy.id, CustomStrategyPosition.status == "OPEN",
        ).all()
        if remaining:
            _close_many(db, strategy, engine, remaining, "TIME_EXIT", mode_label, active_symbol)
        return

    if strategy.status == "PAUSED":
        return
    if _is_blacked_out(rules, today):
        return

    # 5a. Morning entry — hedges + morning shorts, once.
    if not marker.get("hedges_entered") and now_hhmm >= session_cfg.get("morning_entry", "99:99"):
        try:
            expiry = engine.resolve_expiry()
            atm = engine.get_atm_strike()
            chain = engine.get_chain(expiry)
            otm = get_setting(rules, "otm_points")

            hedge_ce_strike = engine.find_hedge_strike(chain, atm, "CE")
            hedge_pe_strike = engine.find_hedge_strike(chain, atm, "PE")
            hedge_ce = engine.buy_hedge(chain, expiry, "CE", hedge_ce_strike)
            hedge_pe = engine.buy_hedge(chain, expiry, "PE", hedge_pe_strike)
            short_ce = engine.sell_short(chain, expiry, "CE", atm + otm)
            short_pe = engine.sell_short(chain, expiry, "PE", atm - otm)
        except (ComplianceError, RuntimeError) as exc:
            log.error("session_seller_engine: morning entry failed for strategy %s (%s): %s", strategy.id, strategy.name, exc)
            notify("custom_strategy", f"Morning entry failed for \"{strategy.name}\" ({mode_label} mode, {active_symbol}): {exc}. Will keep retrying until the morning window closes.", level="warning", user_id=strategy.user_id)
        else:
            for i, (leg, role) in enumerate([(hedge_ce, "HEDGE"), (hedge_pe, "HEDGE"), (short_ce, "SHORT"), (short_pe, "SHORT")]):
                db.add(CustomStrategyPosition(
                    strategy_id=strategy.id, leg_index=i, mode=mode_label,
                    instrument_key=leg["instrument_token"], instrument_type="OPTION", option_type=leg["option_type"],
                    strike=leg["strike"], expiry=leg["expiry"], transaction_type=leg["transaction_type"],
                    quantity=leg["quantity"], entry_price=leg["entry_price"] or 0, order_id=leg.get("order_id"), status="OPEN",
                    leg_config_json=json.dumps({"role": role, "session": "MORNING", "date": today_str, "symbol": active_symbol}),
                ))
            _set_marker(strategy, hedges_entered=True, morning_entered=True)
            db.commit()
            details = f"hedges: CE {hedge_ce['strike']}@{hedge_ce['entry_price'] or 0:.2f} / PE {hedge_pe['strike']}@{hedge_pe['entry_price'] or 0:.2f} | shorts: CE {short_ce['strike']}@{short_ce['entry_price'] or 0:.2f} / PE {short_pe['strike']}@{short_pe['entry_price'] or 0:.2f}"
            notify("custom_strategy", f"Trade Opened — {strategy.name}\n({mode_label})\nMorning session — {details}", level="trade", user_id=strategy.user_id)
            alert_trade_opened(strategy.name, mode_label, active_symbol, f"Morning: {details}")
            log.info("session_seller_engine: morning entry for strategy %s (%s) %s — %s.", strategy.id, strategy.name, active_symbol, details)
        return

    # 5b. Afternoon entry — fresh ATM shorts, reusing the still-open morning hedges.
    if marker.get("hedges_entered") and not marker.get("afternoon_entered") and now_hhmm >= session_cfg.get("afternoon_entry", "99:99"):
        try:
            expiry = engine.resolve_expiry()
            atm = engine.get_atm_strike()
            chain = engine.get_chain(expiry)
            short_ce = engine.sell_short(chain, expiry, "CE", atm)
            short_pe = engine.sell_short(chain, expiry, "PE", atm)
        except (ComplianceError, RuntimeError) as exc:
            log.error("session_seller_engine: afternoon entry failed for strategy %s (%s): %s", strategy.id, strategy.name, exc)
            notify("custom_strategy", f"Afternoon entry failed for \"{strategy.name}\" ({mode_label} mode, {active_symbol}): {exc}. Will keep retrying until the afternoon window closes.", level="warning", user_id=strategy.user_id)
            return

        next_leg_index = max((p.leg_index for p in open_positions), default=-1) + 1
        for i, leg in enumerate((short_ce, short_pe)):
            db.add(CustomStrategyPosition(
                strategy_id=strategy.id, leg_index=next_leg_index + i, mode=mode_label,
                instrument_key=leg["instrument_token"], instrument_type="OPTION", option_type=leg["option_type"],
                strike=leg["strike"], expiry=leg["expiry"], transaction_type=leg["transaction_type"],
                quantity=leg["quantity"], entry_price=leg["entry_price"] or 0, order_id=leg.get("order_id"), status="OPEN",
                leg_config_json=json.dumps({"role": "SHORT", "session": "AFTERNOON", "date": today_str, "symbol": active_symbol}),
            ))
        _set_marker(strategy, afternoon_entered=True)
        db.commit()
        details = f"CE {short_ce['strike']}@{short_ce['entry_price'] or 0:.2f} / PE {short_pe['strike']}@{short_pe['entry_price'] or 0:.2f}"
        notify("custom_strategy", f"Trade Opened — {strategy.name}\n({mode_label})\nAfternoon session — {details}", level="trade", user_id=strategy.user_id)
        alert_trade_opened(strategy.name, mode_label, active_symbol, f"Afternoon: {details}")
        log.info("session_seller_engine: afternoon entry for strategy %s (%s) %s — %s.", strategy.id, strategy.name, active_symbol, details)
