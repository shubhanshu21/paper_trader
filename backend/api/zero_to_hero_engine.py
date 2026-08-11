"""
api/zero_to_hero_engine.py — the SIGNAL-DRIVEN engine for the "Zero to
Hero" PDH/PDL breakout-pullback option BUYING strategy (CustomStrategy
rows with strategy_type == "ZERO_TO_HERO", see strategies/custom/
zero_to_hero_schema.py).

Provides ONLY _tick_one_strategy(db, strategy, brokers) — no background
loop/task here, same as api/intraday_indicator_scheduler.py and every
other signal-driven engine. Registered in api/strategy_scheduler.py's
dispatch table and in strategies/custom/engine_registry.py.

Each entry opens TWO CustomStrategyPosition rows (leg_index 0 = "TP1",
half the lots; leg_index 1 = "RUNNER", the other half) sharing one
sl_index_price/target_index_price pair (stored per-row in
leg_config_json, computed once at signal time) — SL and the 1:1 target
are both price LEVELS on the underlying index (the entry candle's
high/low, same as the reference strategy draws them on the Nifty chart
itself), not on the option's own premium.

Each tick, with position(s) open:
  1. SL — either leg still open whose underlying LTP has reached/crossed
     its stored sl_index_price: close it. If the TP1 leg is closed this
     way (i.e. it never reached its 1:1 target first), today's re-entry
     is allowed; if TP1 was already closed by hitting target, it is not
     (see the day-state marker below).
  2. TP1 — the TP1 leg only, before exit_time: close it once its target
     is reached ("book 50% at 1:1 risk:reward").
  3. Time exit — whatever's still open (normally just the RUNNER leg) at
     exit_time: close it, no overnight positions ever.
With no position open: before exit_time, under the re-entry cap, and
gated on last acting on a DIFFERENT trigger candle than any previous
entry today (so this doesn't immediately re-fire off the exact same
already-used candle before a fresh 15-min bar has even closed) —
evaluate the live signal and BUY the ATM CALL (bullish break) or ATM PUT
(bearish break) if one just fired.
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
from strategies.custom.zero_to_hero_schema import get_setting
from strategies.custom.zero_to_hero_strategy import ZeroToHeroStrategy
from utils.logger import get_logger
from utils.notify import notify
from utils.telegram_alert import alert_trade_closed, alert_trade_opened

log = get_logger(__name__)

_IST = ZoneInfo("Asia/Kolkata")

_audit = AuditTrail(audit_log_path="logs/zero_to_hero_audit.log")
_kill_switch = get_global_kill_switch()


def _mode_for_status(status: str) -> str:
    return "paper" if status == "PAPER_TRADING" else "live"


def _get_brokers() -> dict | None:
    """Reuses the SAME cached broker pair custom_strategy_scheduler.py builds — one live/paper broker pair per process, not a second independent one for this engine."""
    from api.custom_strategy_scheduler import _get_brokers as _shared_get_brokers
    return _shared_get_brokers()


def _get_marker(strategy: CustomStrategy) -> dict:
    """
    Per-day state — {"date", "entries_today", "rr_hit_today",
    "last_trigger_ts"} — persisted the same way weekly_directional_engine.py
    uses strategy.last_entry_date as a JSON scratch column (see that
    module's _get_cycle_marker) rather than adding new DB columns for a
    single-strategy-type concern.
    """
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
    """Today's marker, freshly reset if the stored one is from a prior day (a new trading day resets the re-entry count and the 1:1-reached flag)."""
    today_str = date.today().isoformat()
    marker = _get_marker(strategy)
    if marker.get("date") != today_str:
        return {"date": today_str, "entries_today": 0, "rr_hit_today": False, "last_trigger_ts": None}
    return marker


def _close_leg(db, strategy: CustomStrategy, broker, position: CustomStrategyPosition, trigger: str, now_price: float | None, symbol: str) -> bool:
    """
    Square off ONE open BUY leg — opposite (SELL) MARKET order, same
    atomic OPEN->CLOSING claim (prevents a double-close race) as
    api/intraday_indicator_scheduler.py's own _close_position, adapted
    for a long leg (SELL to close instead of BUY to close).
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
        get_rate_limiter_for(strategy.user_id).acquire()
        exit_order_id = broker.place_sell_order(
            instrument_token=position.instrument_key, quantity=position.quantity, product="MIS",
            order_type="MARKET", tag=f"Z2H_EXIT_{strategy.id}"[:20], user_id=strategy.user_id,
            is_close=True,
        )
    except Exception as exc:
        log.critical(
            "zero_to_hero_engine: FAILED to square off position %s for strategy %s: %s — MANUAL INTERVENTION REQUIRED.",
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
    """One strategy's worth of a tick. Never raises — logs and returns, same discipline every other engine in this app follows so one strategy's failure never stops the rest."""
    if not strategy.rules_json:
        return
    try:
        rules = json.loads(strategy.rules_json)
        symbols = json.loads(strategy.symbols)
        symbol = symbols[0]
    except (json.JSONDecodeError, IndexError):
        return

    exit_time = get_setting(rules, "exit_time")
    max_reentries = get_setting(rules, "max_reentries")

    open_positions = db.query(CustomStrategyPosition).filter(
        CustomStrategyPosition.strategy_id == strategy.id,
        CustomStrategyPosition.status == "OPEN",
    ).all()

    broker_mode = open_positions[0].mode if (strategy.status == "PAUSED" and open_positions) else _mode_for_status(strategy.status)
    broker = brokers.get(broker_mode)
    if broker is None:
        return

    now_hhmm = datetime.now(_IST).strftime("%H:%M")
    marker = _today_marker(strategy)

    # 1. EXIT — SL takes precedence over TP1/time on every leg, then TP1
    #    (only the TP1 leg, only before exit_time), then the time
    #    hard-stop for whatever's still open. All three checks happen
    #    every tick, not just once, since a leg can outlive several ticks.
    if open_positions:
        underlying_key = broker.resolve_instrument_key(symbol)
        spot = broker.get_ltp(underlying_key)
        time_exit = now_hhmm >= exit_time

        for position in open_positions:
            try:
                cfg = json.loads(position.leg_config_json or "{}")
            except json.JSONDecodeError:
                cfg = {}
            sl = cfg.get("sl_index_price")
            target = cfg.get("target_index_price")
            bias = cfg.get("bias")
            role = cfg.get("role")

            if spot is not None and sl is not None and bias is not None:
                sl_hit = (spot >= sl) if bias == "BEARISH" else (spot <= sl)
                if sl_hit:
                    if _close_leg(db, strategy, broker, position, "SL_HIT", spot, symbol) and role == "TP1":
                        # TP1 was stopped out before ever reaching 1:1 — the
                        # reference strategy's re-entry rule is explicitly
                        # gated on this ("if 1:1 RR was already achieved
                        # before SL was hit, do not re-enter").
                        marker["rr_hit_today"] = False
                    continue

            if role == "TP1" and not time_exit and spot is not None and target is not None and bias is not None:
                target_hit = (spot <= target) if bias == "BEARISH" else (spot >= target)
                if target_hit:
                    if _close_leg(db, strategy, broker, position, "PARTIAL_TP_1_1", spot, symbol):
                        marker["rr_hit_today"] = True
                    continue

            if time_exit:
                _close_leg(db, strategy, broker, position, "TIME_EXIT", spot, symbol)

        _set_marker(strategy, **marker)
        db.commit()
        return  # a position was open this tick — never also evaluate a fresh entry

    # 2. ENTRY — PAUSED strategies never take new entries (same contract
    #    every other engine in this app follows).
    if strategy.status == "PAUSED":
        return
    if now_hhmm >= exit_time:
        return  # past today's square-off cutoff — no new entries
    if marker["entries_today"] >= 1 + max_reentries:
        return
    if marker["entries_today"] >= 1 and marker.get("rr_hit_today"):
        return  # 1:1 was already reached on a prior entry today — no re-entry, per the reference strategy's own rule

    try:
        engine = ZeroToHeroStrategy(
            broker=broker, audit=_audit, kill_switch=_kill_switch, rate_limiter=get_rate_limiter_for(strategy.user_id),
            symbol=symbol, rules=rules, user_id=strategy.user_id,
        )
    except Exception as exc:
        log.error("zero_to_hero_engine: could not build engine for strategy %s (%s): %s", strategy.id, strategy.name, exc)
        return

    try:
        signal = engine.evaluate_signal()
    except Exception as exc:
        log.error("zero_to_hero_engine: signal evaluation failed for strategy %s (%s): %s", strategy.id, strategy.name, exc)
        return
    if signal is None or signal["signal"] == "NONE":
        return
    if signal["trigger_timestamp"] == marker.get("last_trigger_ts"):
        return  # already acted on this exact candle — avoids an instant re-entry off the same historical bar before a fresh one has closed

    lots = rules.get("lots", 2)
    tp1_lots, runner_lots = lots // 2, lots - lots // 2
    tp1_qty, runner_qty = tp1_lots * engine.real_lot_size, runner_lots * engine.real_lot_size

    mode = _mode_for_status(strategy.status)
    common_cfg = {
        "sl_index_price": signal["sl_index_price"], "target_index_price": signal["target_index_price"],
        "entry_index_price": signal["entry_index_price"], "bias": signal["signal"],
    }

    try:
        tp1_leg = engine.enter(signal["option_type"], tp1_qty)
    except (ComplianceError, RuntimeError) as exc:
        log.error("zero_to_hero_engine: entry failed for strategy %s (%s): %s", strategy.id, strategy.name, exc)
        notify(
            "custom_strategy",
            f"Entry failed for strategy \"{strategy.name}\" ({mode} mode): {exc}. Will keep retrying while a signal is active.",
            level="warning", user_id=strategy.user_id,
        )
        return

    db.add(CustomStrategyPosition(
        strategy_id=strategy.id, leg_index=0, mode=mode,
        instrument_key=tp1_leg["instrument_token"], instrument_type="OPTION",
        option_type=tp1_leg["option_type"], strike=tp1_leg["strike"], expiry=tp1_leg["expiry"],
        transaction_type="BUY", quantity=tp1_qty, entry_price=tp1_leg["entry_price"] or 0,
        order_id=tp1_leg.get("order_id"), status="OPEN",
        leg_config_json=json.dumps({**common_cfg, "role": "TP1"}),
    ))
    db.commit()

    try:
        runner_leg = engine.enter(signal["option_type"], runner_qty)
    except (ComplianceError, RuntimeError) as exc:
        # The TP1 half is ALREADY filled and committed above — this is a
        # genuine partial-fill state, not a clean "entry failed" one. Log
        # loudly and let the strategy carry on managing the TP1 leg it
        # does have rather than trying to auto-unwind it (the codebase's
        # other partial-fill handling is basket-shaped; this is a single
        # extra leg of the SAME instrument, safe to just hold).
        log.critical(
            "zero_to_hero_engine: TP1 leg filled but RUNNER leg failed for strategy %s (%s): %s — "
            "continuing with the TP1 half only.", strategy.id, strategy.name, exc,
        )
        notify(
            "custom_strategy",
            f"\"{strategy.name}\" ({mode} mode): the runner half of today's entry failed to place ({exc}). "
            f"The booked half is still open and being managed normally.",
            level="warning", user_id=strategy.user_id,
        )
        marker["entries_today"] += 1
        marker["rr_hit_today"] = False
        marker["last_trigger_ts"] = signal["trigger_timestamp"]
        _set_marker(strategy, **marker)
        db.commit()
        details = f"BUY {tp1_leg['option_type']} {tp1_leg['strike']}@{tp1_leg['entry_price'] or 0:.2f} | qty={tp1_qty} (TP1 half only) | signal={signal['signal']}"
        notify("custom_strategy", f"Trade Opened — {strategy.name}\n({mode})\n{details}", level="trade", user_id=strategy.user_id)
        alert_trade_opened(strategy.name, mode, symbol, details)
        return

    db.add(CustomStrategyPosition(
        strategy_id=strategy.id, leg_index=1, mode=mode,
        instrument_key=runner_leg["instrument_token"], instrument_type="OPTION",
        option_type=runner_leg["option_type"], strike=runner_leg["strike"], expiry=runner_leg["expiry"],
        transaction_type="BUY", quantity=runner_qty, entry_price=runner_leg["entry_price"] or 0,
        order_id=runner_leg.get("order_id"), status="OPEN",
        leg_config_json=json.dumps({**common_cfg, "role": "RUNNER"}),
    ))

    marker["entries_today"] += 1
    marker["rr_hit_today"] = False
    marker["last_trigger_ts"] = signal["trigger_timestamp"]
    _set_marker(strategy, **marker)
    db.commit()

    details = (
        f"BUY {tp1_leg['option_type']} {tp1_leg['strike']}@{tp1_leg['entry_price'] or 0:.2f} | "
        f"qty={tp1_qty + runner_qty} (split {tp1_qty}/{runner_qty}) | signal={signal['signal']}"
    )
    notify("custom_strategy", f"Trade Opened — {strategy.name}\n({mode})\n{details}", level="trade", user_id=strategy.user_id)
    alert_trade_opened(strategy.name, mode, symbol, details)
    log.info("zero_to_hero_engine: entered strategy %s (%s) — %s.", strategy.id, strategy.name, details)
