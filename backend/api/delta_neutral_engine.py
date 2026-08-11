"""
api/delta_neutral_engine.py — the DELTA-DRIVEN ADJUSTABLE STRANGLE engine
for the Monthly Delta-Neutral Strangle with Dynamic Adjustment strategy
(CustomStrategy rows with strategy_type == "DELTA_NEUTRAL_STRANGLE", see
strategies/custom/delta_neutral_schema.py).

Provides ONLY _tick_one_strategy(db, strategy, brokers) — no background
loop/task here, same as every other genuinely-new engine in this app.
Registered in api/strategy_scheduler.py's dispatch table and in
strategies/custom/engine_registry.py.

Up to 4 legs open at once (short CE, short PE, hedge CE, hedge PE). Each
tick, with a position open:
  1. 3rd-weekly-expiry time exit — unconditional, regardless of
     target/adjustment state (see delta_neutral_schema.py for why the
     3rd, never the 4th/last, weekly of the month).
  2. Target — cumulative P&L reaching target_capital_pct of the REAL
     broker margin captured at entry.
  3. Stage 2 (checked BEFORE Stage 1 — more severe, and can fire even if
     Stage 1 never did): the LOSING short leg's live |delta| has entered
     [delta_trigger_min, delta_trigger_max] -> close EVERYTHING, re-sell
     both legs chosen by PREMIUM (reset_premium_pct of the losing leg's
     own exit price), plus fresh hedges. Only ever fires once per cycle.
  4. Stage 1 (only if Stage 2 hasn't already fired this cycle): the
     short-leg premium ratio has reached premium_ratio_trigger -> close
     the WINNING (cheaper) short leg only (hedges untouched), re-sell it
     matching the LOSING leg's current live delta. Only ever fires once
     per cycle.
With no position open: enter all 4 legs once resolve_expiry() rolls to a
new cycle and the configured entry_time-entry_time_end window is open.
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
from strategies.custom.delta_neutral_schema import get_setting
from strategies.custom.delta_neutral_strategy import DeltaNeutralStrategy
from utils.instrument_cache import InstrumentCache
from utils.logger import get_logger
from utils.notify import notify
from utils.telegram_alert import alert_trade_closed, alert_trade_opened

log = get_logger(__name__)

_IST = ZoneInfo("Asia/Kolkata")

_audit = AuditTrail(audit_log_path="logs/delta_neutral_audit.log")
_kill_switch = get_global_kill_switch()


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
        total += _leg_pnl(p, None if p.status == "CLOSED" else broker.get_ltp(p.instrument_key))
    return total


def _close_position(db, strategy: CustomStrategy, engine: DeltaNeutralStrategy, position: CustomStrategyPosition, trigger: str) -> dict | None:
    """Returns the close result ({"exit_price":..., "order_id":...}) on success, None on failure/already-claimed."""
    from sqlalchemy import update

    claimed = db.execute(
        update(CustomStrategyPosition)
        .where(CustomStrategyPosition.id == position.id, CustomStrategyPosition.status == "OPEN")
        .values(status="CLOSING")
    ).rowcount
    db.commit()
    if claimed == 0:
        return None

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
                "delta_neutral_engine: leg %s for strategy %s expired/delisted and spot LTP for %s is also "
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
            return None

        strike = float(position.strike)
        intrinsic = max(spot - strike, 0.0) if position.option_type == "CE" else max(strike - spot, 0.0)
        log.warning(
            "delta_neutral_engine: leg %s for strategy %s expired/delisted — settling at intrinsic value %.2f "
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
        return {"exit_price": intrinsic, "order_id": None}

    try:
        result = engine.close_leg(position.instrument_key, position.quantity, float(position.strike), position.option_type, position.transaction_type)
    except Exception as exc:
        log.critical("delta_neutral_engine: FAILED to close leg %s for strategy %s: %s — MANUAL INTERVENTION REQUIRED.", position.instrument_key, strategy.id, exc)
        notify(
            "custom_strategy",
            f"MANUAL INTERVENTION REQUIRED — failed to close \"{strategy.name}\" leg {position.instrument_key} "
            f"({trigger} triggered): {exc}. A position may still be open on your real account.",
            user_id=strategy.user_id,
        )
        position.status = "OPEN"
        db.commit()
        return None

    position.status = "CLOSED"
    position.exit_price = result["exit_price"]
    position.exit_order_id = result["order_id"]
    position.exit_reason = trigger
    position.closed_at = datetime.now()
    db.commit()
    return result


def _close_all(db, strategy: CustomStrategy, engine: DeltaNeutralStrategy, positions: list[CustomStrategyPosition], trigger: str, mode_label: str, symbol: str) -> None:
    closed_any = False
    for position in positions:
        if _close_position(db, strategy, engine, position, trigger) is not None:
            closed_any = True
    if closed_any:
        details = f"reason={trigger} | {len(positions)} legs squared off"
        notify("custom_strategy", f"Trade Closed — {strategy.name}\n({mode_label})\n{details}", level="trade", user_id=strategy.user_id)
        alert_trade_closed(strategy.name, mode_label, symbol, details)
        log.info("delta_neutral_engine: closed all legs for strategy %s (%s) | trigger=%s", strategy.id, strategy.name, trigger)


def _add_leg(db, strategy: CustomStrategy, leg: dict, leg_index: int, mode_label: str) -> None:
    db.add(CustomStrategyPosition(
        strategy_id=strategy.id, leg_index=leg_index, mode=mode_label,
        instrument_key=leg["instrument_token"], instrument_type="OPTION", option_type=leg["option_type"],
        strike=leg["strike"], expiry=leg["expiry"], transaction_type=leg["transaction_type"],
        quantity=leg["quantity"], entry_price=leg["entry_price"] or 0, order_id=leg.get("order_id"), status="OPEN",
        leg_config_json=json.dumps({"role": leg["role"]}),
    ))


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
        engine = DeltaNeutralStrategy(broker=broker, audit=_audit, kill_switch=_kill_switch, rate_limiter=get_rate_limiter_for(strategy.user_id), symbol=symbol, rules=rules, user_id=strategy.user_id)
    except Exception as exc:
        log.error("delta_neutral_engine: could not build engine for strategy %s (%s): %s", strategy.id, strategy.name, exc)
        return

    mode_label = _mode_for_status(strategy.status)
    marker = _get_marker(strategy)

    # 1-4. EXIT / ADJUST — a position is open.
    if open_positions:
        expiry = open_positions[0].expiry

        third_weekly = marker.get("third_weekly_expiry")
        if not third_weekly:
            try:
                third_weekly = engine.resolve_third_weekly_expiry(expiry)
                _set_marker(strategy, third_weekly_expiry=third_weekly)
                db.commit()
            except Exception as exc:
                log.warning("delta_neutral_engine: could not resolve 3rd weekly expiry for strategy %s (%s): %s", strategy.id, strategy.name, exc)

        exit_time = get_setting(rules, "third_weekly_exit_time")
        if third_weekly and datetime.now(_IST).date().isoformat() == third_weekly and datetime.now(_IST).strftime("%H:%M") >= exit_time:
            _close_all(db, strategy, engine, open_positions, "TIME_EXIT", mode_label, symbol)
            return

        margin_at_entry = marker.get("margin")
        if margin_at_entry:
            pnl = _cycle_pnl(db, strategy.id, expiry, broker)
            if pnl >= margin_at_entry * get_setting(rules, "target_capital_pct") / 100.0:
                _close_all(db, strategy, engine, open_positions, "TAKE_PROFIT", mode_label, symbol)
                return

        short_ce = next((p for p in open_positions if p.option_type == "CE" and _leg_meta(p).get("role") == "SHORT"), None)
        short_pe = next((p for p in open_positions if p.option_type == "PE" and _leg_meta(p).get("role") == "SHORT"), None)
        if short_ce is None or short_pe is None:
            return  # mid-adjustment — resume once both are back

        ce_premium, pe_premium = broker.get_ltp(short_ce.instrument_key), broker.get_ltp(short_pe.instrument_key)
        if not ce_premium or not pe_premium:
            return

        try:
            futures_price = engine.get_futures_price()
        except Exception as exc:
            log.warning("delta_neutral_engine: could not fetch futures price for strategy %s (%s): %s", strategy.id, strategy.name, exc)
            return
        ce_delta = engine.live_delta(float(short_ce.strike), "CE", ce_premium, expiry, futures_price)
        pe_delta = engine.live_delta(float(short_pe.strike), "PE", pe_premium, expiry, futures_price)
        if ce_delta is None or pe_delta is None:
            return

        losing_is_ce = ce_delta >= pe_delta
        losing_delta = ce_delta if losing_is_ce else pe_delta
        stage = marker.get("adjustment_stage", 0)

        # 3. Stage 2 — checked first (more severe, independent of Stage 1).
        dmin, dmax = get_setting(rules, "delta_trigger_min"), get_setting(rules, "delta_trigger_max")
        if stage < 2 and dmin <= losing_delta <= dmax:
            losing_leg = short_ce if losing_is_ce else short_pe
            all_legs = list(open_positions)
            closed_results = {}
            all_closed = True
            for leg in all_legs:
                result = _close_position(db, strategy, engine, leg, "ADJUSTMENT_STAGE_2")
                if result is None:
                    all_closed = False
                else:
                    closed_results[leg.id] = result
            if not all_closed:
                return  # some legs failed to close — logged/notified inside; retry next tick
            losing_exit_price = closed_results[losing_leg.id]["exit_price"] or 0.0
            target_premium = losing_exit_price * get_setting(rules, "reset_premium_pct") / 100.0

            try:
                atm = engine.get_atm_strike()
                chain = engine.get_chain(expiry)
                new_ce_strike = engine.find_strike_by_premium(chain, "CE", atm, target_premium)
                new_pe_strike = engine.find_strike_by_premium(chain, "PE", atm, target_premium)
                hedge_ce_strike = engine.find_hedge_strike(chain, atm, "CE")
                hedge_pe_strike = engine.find_hedge_strike(chain, atm, "PE")
                new_legs = [
                    engine.sell_short(chain, expiry, "CE", new_ce_strike),
                    engine.sell_short(chain, expiry, "PE", new_pe_strike),
                    engine.buy_hedge(chain, expiry, "CE", hedge_ce_strike),
                    engine.buy_hedge(chain, expiry, "PE", hedge_pe_strike),
                ]
            except (ComplianceError, RuntimeError) as exc:
                log.error("delta_neutral_engine: Stage 2 reset failed for strategy %s (%s): %s", strategy.id, strategy.name, exc)
                notify("custom_strategy", f"Stage 2 reset failed for \"{strategy.name}\" ({mode_label} mode): {exc}. The position is now FLAT until the next tick retries.", level="warning", user_id=strategy.user_id)
                return

            for i, leg in enumerate(new_legs):
                _add_leg(db, strategy, leg, i, mode_label)
            _set_marker(strategy, adjustment_stage=2)
            db.commit()
            details = f"target premium ₹{target_premium:.2f} (50% of losing leg's ₹{losing_exit_price:.2f} exit) — new CE {new_ce_strike}, new PE {new_pe_strike}"
            notify("custom_strategy", f"Trade Adjusted — {strategy.name}\n({mode_label})\nStage 2 full reset — {details}", level="trade", user_id=strategy.user_id)
            alert_trade_opened(strategy.name, mode_label, symbol, f"Stage 2 reset: {details}")
            log.info("delta_neutral_engine: Stage 2 reset for strategy %s (%s) — %s.", strategy.id, strategy.name, details)
            return

        # 4. Stage 1 — only if Stage 2 hasn't already fired.
        ratio = max(ce_premium, pe_premium) / min(ce_premium, pe_premium)
        if stage < 1 and ratio >= get_setting(rules, "premium_ratio_trigger"):
            winning_leg = short_pe if losing_is_ce else short_ce
            winning_option_type = "PE" if losing_is_ce else "CE"

            if _close_position(db, strategy, engine, winning_leg, "ADJUSTMENT_STAGE_1") is None:
                return  # close failed — retry next tick

            try:
                atm = engine.get_atm_strike()
                chain = engine.get_chain(expiry)
                new_strike = engine.find_strike_by_delta(chain, winning_option_type, atm, expiry, futures_price, losing_delta)
                new_leg = engine.sell_short(chain, expiry, winning_option_type, new_strike)
            except (ComplianceError, RuntimeError) as exc:
                log.error("delta_neutral_engine: Stage 1 rebalance failed for strategy %s (%s): %s", strategy.id, strategy.name, exc)
                notify("custom_strategy", f"Stage 1 rebalance failed for \"{strategy.name}\" ({mode_label} mode): {exc}. The {winning_option_type} side is now FLAT until the next tick retries.", level="warning", user_id=strategy.user_id)
                return

            next_leg_index = max((p.leg_index for p in open_positions), default=-1) + 1
            _add_leg(db, strategy, new_leg, next_leg_index, mode_label)
            _set_marker(strategy, adjustment_stage=1)
            db.commit()
            details = f"booked {winning_option_type} {winning_leg.strike}, re-sold {winning_option_type} {new_strike} matching the losing leg's {losing_delta:.3f} delta"
            notify("custom_strategy", f"Trade Adjusted — {strategy.name}\n({mode_label})\nStage 1 rebalance — {details}", level="trade", user_id=strategy.user_id)
            alert_trade_opened(strategy.name, mode_label, symbol, f"Stage 1 rebalance: {details}")
            log.info("delta_neutral_engine: Stage 1 rebalance for strategy %s (%s) — %s.", strategy.id, strategy.name, details)
        return

    # ENTRY — no open position.
    if strategy.status == "PAUSED":
        return

    try:
        current_expiry = engine.resolve_expiry()
    except Exception as exc:
        log.warning("delta_neutral_engine: could not resolve monthly expiry for strategy %s (%s): %s", strategy.id, strategy.name, exc)
        return
    if marker.get("expiry") == current_expiry:
        return  # already traded this cycle

    now_hhmm = datetime.now(_IST).strftime("%H:%M")
    entry_start, entry_end = get_setting(rules, "entry_time"), get_setting(rules, "entry_time_end")
    if not (entry_start <= now_hhmm < entry_end):
        return

    try:
        legs = engine.enter(current_expiry)
    except (ComplianceError, RuntimeError) as exc:
        log.error("delta_neutral_engine: entry failed for strategy %s (%s): %s", strategy.id, strategy.name, exc)
        notify("custom_strategy", f"Entry failed for strategy \"{strategy.name}\" ({mode_label} mode): {exc}. Will keep retrying until today's entry window closes.", level="warning", user_id=strategy.user_id)
        return

    margin_at_entry = None
    try:
        basket = [{"instrument_key": leg["instrument_token"], "quantity": leg["quantity"], "transaction_type": leg["transaction_type"], "product": "D"} for leg in legs]
        margin_at_entry = broker.get_basket_required_margin(basket)
    except Exception as exc:
        log.warning("delta_neutral_engine: basket margin lookup failed for strategy %s (%s): %s", strategy.id, strategy.name, exc)

    try:
        third_weekly = engine.resolve_third_weekly_expiry(current_expiry)
    except Exception as exc:
        log.warning("delta_neutral_engine: could not resolve 3rd weekly expiry for strategy %s (%s): %s", strategy.id, strategy.name, exc)
        third_weekly = None

    _set_marker(strategy, expiry=current_expiry, date=datetime.now(_IST).date().isoformat(), margin=margin_at_entry, adjustment_stage=0, third_weekly_expiry=third_weekly)
    for i, leg in enumerate(legs):
        _add_leg(db, strategy, leg, i, mode_label)
    db.commit()

    details = " | ".join(f"{leg['role']} {leg['option_type']} {leg['strike']}@{leg['entry_price'] or 0:.2f}" for leg in legs)
    notify("custom_strategy", f"Trade Opened — {strategy.name}\n({mode_label})\n{details}", level="trade", user_id=strategy.user_id)
    alert_trade_opened(strategy.name, mode_label, symbol, details)
    log.info("delta_neutral_engine: entered strategy %s (%s) — %s.", strategy.id, strategy.name, details)
