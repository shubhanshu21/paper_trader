"""
api/weekend_combo_scheduler.py — the MULTI-SYMBOL-COMBINED engine for the
Nifty + Sensex weekend-gap combo (CustomStrategy rows with strategy_type
== "WEEKEND_GAP_COMBO", see strategies/custom/combo_schema.py).

A separate engine, not an extension of custom_strategy_scheduler.py or
intraday_indicator_scheduler.py — this strategy shape breaks
custom_strategy_scheduler.py's "one symbol per basket" assumption (it
trades TWO different symbols, opposite bias, as ONE combined position)
and has nothing to do with intraday_indicator_scheduler.py's live-signal/
multi-entry-per-day model at all — it enters once a week, on a fixed
weekday/time.

This module no longer owns a background task/event loop itself — see
api/strategy_scheduler.py, the single shared scheduler that ticks every
CustomStrategy row (any strategy_type) and dispatches WEEKEND_GAP_COMBO
rows to _tick_one_strategy below. Only the loop was unified; the
entry/exit logic here is unchanged.

Each tick (called from strategy_scheduler.py, only during real NSE market hours):
  1. EXIT — for every strategy with OPEN positions (across BOTH symbols):
     close everything if the configured exit_weekday/exit_time has been
     reached (hard stop — no exceptions), or if the COMBINED P&L across
     every open leg of both symbols together hits +target_amount or
     -stop_loss_amount.
  2. ENTRY — for every PAPER_TRADING/LIVE strategy (PAUSED skipped, same
     convention as the other schedulers) with NO open position, on/after
     the configured entry_weekday+entry_time, and not already entered
     this week (tracked via last_entry_date, same JSON-blob-reuse pattern
     custom_strategy_scheduler.py already uses for its own cycle
     tracking): resolve the CURRENTLY configured `bias` (the user may
     have edited it since the strategy was built — see combo_schema.py's
     module docstring for why that's intentional) and enter all six legs.
"""
import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from compliance.sebi_rules import (
    AuditTrail,
    ComplianceError,
    assert_market_is_open,
    get_global_kill_switch,
    get_rate_limiter_for,
)
from db.models import CustomStrategy, CustomStrategyPosition
from strategies.custom.combo_schema import get_setting
from strategies.custom.weekend_combo_strategy import WeekendGapComboStrategy
from utils.logger import get_logger
from utils.notify import notify
from utils.telegram_alert import alert_trade_closed, alert_trade_opened

log = get_logger(__name__)

# Tick cadence now lives in api/strategy_scheduler.py, the single shared
# loop that calls _tick_one_strategy below.
_IST = ZoneInfo("Asia/Kolkata")
_WEEKDAY_NUMS = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}

_audit = AuditTrail(audit_log_path="logs/weekend_combo_audit.log")
_kill_switch = get_global_kill_switch()


def _market_is_open_now() -> bool:
    try:
        assert_market_is_open()
        return True
    except RuntimeError:
        return False


def _mode_for_status(status: str) -> str:
    return "paper" if status == "PAPER_TRADING" else "live"


def _get_brokers() -> dict | None:
    """Reuses the SAME cached broker pair every scheduler in this process shares."""
    from api.custom_strategy_scheduler import _get_brokers as _shared_get_brokers
    return _shared_get_brokers()


def _last_entered_friday(strategy: CustomStrategy) -> str | None:
    if not strategy.last_entry_date:
        return None
    try:
        data = json.loads(strategy.last_entry_date)
    except json.JSONDecodeError:
        return None
    return data.get("entry_date") if isinstance(data, dict) else None


def _set_last_entered_friday(strategy: CustomStrategy, entry_date: str) -> None:
    strategy.last_entry_date = json.dumps({"entry_date": entry_date})


def _close_all_positions(db, strategy: CustomStrategy, broker, positions: list[CustomStrategyPosition], trigger: str, now_prices: dict) -> list[CustomStrategyPosition]:
    """Square off every given OPEN position — opposite MARKET order each, atomic OPEN->CLOSING claim per leg (same race-safety as the other schedulers' _close_leg). Returns the legs actually closed."""
    from sqlalchemy import update

    closed: list[CustomStrategyPosition] = []
    for position in positions:
        claimed = db.execute(
            update(CustomStrategyPosition)
            .where(CustomStrategyPosition.id == position.id, CustomStrategyPosition.status == "OPEN")
            .values(status="CLOSING")
        ).rowcount
        db.commit()
        if claimed == 0:
            continue

        opposite = broker.place_buy_order if position.transaction_type == "SELL" else broker.place_sell_order
        try:
            get_rate_limiter_for(strategy.user_id).acquire()
            exit_order_id = opposite(
                instrument_token=position.instrument_key, quantity=position.quantity, product="NRML",
                order_type="MARKET", tag=f"COMBO_EXIT_{strategy.id}_{position.leg_index}"[:20], user_id=strategy.user_id,
                is_close=True,
            )
        except Exception as exc:
            log.critical(
                "weekend_combo_scheduler: FAILED to square off leg %s for strategy %s: %s — MANUAL INTERVENTION REQUIRED.",
                position.instrument_key, strategy.id, exc,
            )
            notify(
                "custom_strategy",
                f"MANUAL INTERVENTION REQUIRED — failed to exit \"{strategy.name}\" leg {position.instrument_key} "
                f"({trigger} triggered): {exc}. A position may still be open on your real account.",
                user_id=strategy.user_id,
            )
            position.status = "OPEN"  # release the claim so the next tick retries
            db.commit()
            continue

        position.status = "CLOSED"
        fill_price = broker.get_fill_price(exit_order_id) if exit_order_id else None
        position.exit_price = fill_price if fill_price is not None else now_prices.get(position.instrument_key)
        position.exit_order_id = exit_order_id
        position.exit_reason = trigger
        position.closed_at = datetime.now()
        db.commit()
        closed.append(position)
    return closed


def _tick_one_strategy(db, strategy: CustomStrategy, brokers: dict) -> None:
    """One strategy's worth of a tick. Never raises — logs and returns, same discipline as the other schedulers."""
    if not strategy.rules_json:
        return
    try:
        rules = json.loads(strategy.rules_json)
    except json.JSONDecodeError:
        return

    broker_mode = _mode_for_status(strategy.status) if strategy.status != "PAUSED" else None
    open_positions = db.query(CustomStrategyPosition).filter(
        CustomStrategyPosition.strategy_id == strategy.id,
        CustomStrategyPosition.status == "OPEN",
    ).all()
    if strategy.status == "PAUSED":
        broker_mode = open_positions[0].mode if open_positions else None
    if broker_mode is None:
        return
    broker = brokers.get(broker_mode)
    if broker is None:
        return

    now = datetime.now(_IST)
    now_hhmm = now.strftime("%H:%M")
    today = now.date()
    today_weekday = now.strftime("%a").upper()[:3]

    entry_weekday = get_setting(rules, "entry_weekday")
    exit_weekday = get_setting(rules, "exit_weekday")
    exit_time = get_setting(rules, "exit_time")
    target_amount = get_setting(rules, "target_amount")
    stop_loss_amount = get_setting(rules, "stop_loss_amount")

    # 1. EXIT — hard weekday+time stop takes precedence, then combined P&L.
    if open_positions:
        tokens = [p.instrument_key for p in open_positions]
        now_prices = broker.get_ltp_batch(tokens)

        # Computed from the ACTUAL entry date + calendar days to
        # exit_weekday, not a plain "today's weekday string == exit_weekday"
        # match — that would get permanently stuck holding the position if
        # the scheduler is down (or the market's shut for a holiday) on the
        # exact exit day. `today >= exit_date` instead fires on the exit
        # day OR any day after it, same "never miss a deadline, only ever
        # late" discipline BEFORE_EXPIRY's fallback uses elsewhere.
        entry_date_str = _last_entered_friday(strategy)
        trigger = None
        if entry_date_str:
            entry_date = date.fromisoformat(entry_date_str)
            days_to_exit = (_WEEKDAY_NUMS[exit_weekday] - _WEEKDAY_NUMS[entry_weekday]) % 7
            exit_date = entry_date + timedelta(days=days_to_exit)
            if today > exit_date or (today == exit_date and now_hhmm >= exit_time):
                trigger = "TIME_EXIT"
        elif today_weekday == exit_weekday and now_hhmm >= exit_time:
            trigger = "TIME_EXIT"  # no recorded entry date (shouldn't normally happen) — fall back to the simple weekday match
        else:
            from api.custom_strategy_scheduler import _combined_pnl
            from utils.wallet import get_charge_rates
            rates = get_charge_rates(strategy.user_id)
            pnl_result = _combined_pnl(open_positions, now_prices, rates)
            if pnl_result is not None:
                pnl_amount, _pnl_pct = pnl_result
                if pnl_amount >= target_amount:
                    trigger = "TAKE_PROFIT"
                elif pnl_amount <= -stop_loss_amount:
                    trigger = "STOP_LOSS"

        if trigger:
            closed = _close_all_positions(db, strategy, broker, open_positions, trigger, now_prices)
            if closed and len(closed) == len(open_positions):
                leg_details = " | ".join(f"{p.transaction_type} {p.option_type} {p.strike}@{p.exit_price or 0:.2f}" for p in closed)
                details = f"reason={trigger} | {leg_details}"
                mode_label = _mode_for_status(strategy.status)
                symbols = json.loads(strategy.symbols) if strategy.symbols else ["NIFTY", "SENSEX"]
                notify("custom_strategy", f"Trade Closed — {strategy.name}\n({mode_label})\n{details}", level="trade", user_id=strategy.user_id)
                alert_trade_closed(strategy.name, mode_label, "+".join(symbols), details)
                log.info("weekend_combo_scheduler: closed strategy %s (%s) | trigger=%s", strategy.id, strategy.name, trigger)
        return  # a position was open this tick — never also evaluate a fresh entry

    # 2. ENTRY — PAUSED never takes new entries.
    if strategy.status == "PAUSED":
        return

    entry_weekday = get_setting(rules, "entry_weekday")
    entry_time = get_setting(rules, "entry_time")
    if today_weekday != entry_weekday or now_hhmm < entry_time:
        return
    today_iso = date.today().isoformat()
    if _last_entered_friday(strategy) == today_iso:
        return  # already entered this week

    bias = rules.get("bias")
    try:
        engine = WeekendGapComboStrategy(
            broker=broker, audit=_audit, kill_switch=_kill_switch, rate_limiter=get_rate_limiter_for(strategy.user_id),
            rules=rules, user_id=strategy.user_id,
        )
        filled = engine.enter(bias)
    except (ComplianceError, RuntimeError) as exc:
        log.error("weekend_combo_scheduler: entry failed for strategy %s (%s): %s", strategy.id, strategy.name, exc)
        notify(
            "custom_strategy",
            f"Entry failed for strategy \"{strategy.name}\" ({_mode_for_status(strategy.status)} mode): {exc}. "
            f"Will keep retrying every tick until the next entry window.",
            level="warning", user_id=strategy.user_id,
        )
        return

    _set_last_entered_friday(strategy, today_iso)
    db_mode = _mode_for_status(strategy.status)
    for idx, leg in enumerate(filled):
        db.add(CustomStrategyPosition(
            strategy_id=strategy.id, leg_index=idx, mode=db_mode,
            instrument_key=leg["instrument_token"], instrument_type="OPTION",
            option_type=leg["option_type"], strike=leg["strike"], expiry=leg["expiry"],
            transaction_type=leg["action"], quantity=leg["quantity"], entry_price=leg["entry_price"] or 0,
            order_id=leg.get("order_id"), status="OPEN",
        ))
    db.commit()

    leg_details = " | ".join(f"{leg['symbol']} {leg['action']} {leg['option_type']} {leg['strike']}@{leg['entry_price'] or 0:.2f}" for leg in filled)
    details = f"bias={bias} | {leg_details}"
    notify("custom_strategy", f"Trade Opened — {strategy.name}\n({db_mode})\n{details}", level="trade", user_id=strategy.user_id)
    alert_trade_opened(strategy.name, db_mode, "NIFTY+SENSEX", details)
    log.info("weekend_combo_scheduler: entered strategy %s (%s) — %s.", strategy.id, strategy.name, details)
