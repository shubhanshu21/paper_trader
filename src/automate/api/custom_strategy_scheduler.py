"""
api/custom_strategy_scheduler.py — runs paper/live execution for
user-built custom strategies (CustomStrategy rows with status
PAPER_TRADING or LIVE) as a background asyncio task inside the SAME
FastAPI process (started from main.py's startup hook, exactly like
market_broadcaster.py/the daemon supervisor) — per the standing rule in
this codebase: no second systemd service for background work.

This is deliberately independent of run_daemon.py/run_strategy.py (the
.env/STRATEGY_CONFIGS-driven system for hand-written strategies like
TenPercentOTMStrangle) — those are keyed by a static strategy name, not a
DB row a user can create/edit/delete at runtime. Custom strategies get
their own lightweight scheduler here instead of being forced through
that static config path.

Each tick (every _TICK_SEC, only during real NSE market hours):
  1. ENTRY — for every PAPER_TRADING/LIVE strategy with no OPEN basket
     and no basket already entered for the CURRENT expiry cycle, run its
     RuleBasedStrategy once and record the resulting legs as OPEN
     CustomStrategyPosition rows. "Current cycle" is tracked per symbol,
     not per calendar day — a strategy that exits early (TP/SL hit well
     before expiry) must NOT re-enter a fresh basket on the same
     soon-to-expire contract the next morning; it waits until the
     resolved nearest expiry actually rolls over to the next one (see
     _resolve_current_expiry/_get_last_entered_expiry). This is what
     makes "enter once, hold through the cycle, don't come back until
     the next cycle starts" the real behavior, for every entry mode.
  2. EXIT — for every strategy with an OPEN basket, price it and check
     take-profit / stop-loss / exit-time / exit-days-before-expiry;
     square off every leg and mark the basket CLOSED if any trigger fires.
"""
import asyncio
import json
from datetime import date, datetime
from typing import Optional

from automate.compliance.sebi_rules import AuditTrail, KillSwitch, OrderRateLimiter, assert_market_is_open
from automate.db.engine import SessionLocal
from automate.db.models import CustomStrategy, CustomStrategyPosition
from automate.strategies.custom.rule_strategy import RuleBasedStrategy
from automate.utils.logger import get_logger
from automate.utils.notify import notify
from automate.utils.option_utils import check_exit_trigger, is_within_pre_expiry_buffer

log = get_logger(__name__)

_TICK_SEC_OPEN = 60
_TICK_SEC_CLOSED = 300

_audit = AuditTrail(audit_log_path="logs/custom_strategy_audit.log")
_kill_switch = KillSwitch()
_rate_limiter = OrderRateLimiter(max_per_second=10)
_brokers: Optional[dict] = None


def _market_is_open_now() -> bool:
    try:
        assert_market_is_open()
        return True
    except RuntimeError:
        return False


def _get_brokers() -> Optional[dict]:
    """Lazily build the paper/live broker pair; retried every tick until it succeeds (e.g. token not ready yet)."""
    global _brokers
    if _brokers is not None:
        return _brokers
    try:
        from automate.broker.broker_factory import BrokerFactory
        _brokers = BrokerFactory.create_mode_brokers()
    except Exception as exc:
        log.warning("custom_strategy_scheduler: broker init not ready yet (%s) — will retry.", exc)
        return None
    return _brokers


def _mode_for_status(status: str) -> str:
    return "paper" if status == "PAPER_TRADING" else "live"


import re
from collections import defaultdict

def _is_leg_for_symbol(instrument_key: str, symbol: str) -> bool:
    if "|" not in instrument_key:
        return False
    underlying_part = instrument_key.split("|", 1)[1]
    if underlying_part == symbol:
        return True
    # Option contract format: SYMBOL followed by 2-digit year (e.g. NIFTY26...)
    pattern = re.compile(rf"^{re.escape(symbol)}\d{{2}}")
    return bool(pattern.match(underlying_part))


def _get_last_entered_expiry(strategy: CustomStrategy, symbol: str) -> Optional[str]:
    """
    The expiry date this symbol's basket was last entered for, or None.
    Stored in the same last_entry_date JSON column (name kept for
    backward compatibility — no migration needed) but the value per
    symbol is now {"expiry": "YYYY-MM-DD"} rather than a bare calendar
    date string, since re-entry must be gated on the expiry CYCLE, not
    the day of the week (see module docstring). A legacy plain date
    string (pre-existing rows, or the old bare-string-per-symbol shape)
    is treated as "no expiry recorded yet" — worst case one extra entry
    attempt gets made once, which correctly checks has_open_position
    first anyway.
    """
    if not strategy.last_entry_date:
        return None
    try:
        data = json.loads(strategy.last_entry_date)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    entry = data.get(symbol)
    return entry.get("expiry") if isinstance(entry, dict) else None


def _set_last_entered_expiry(strategy: CustomStrategy, symbol: str, expiry: str) -> None:
    try:
        data = json.loads(strategy.last_entry_date) if strategy.last_entry_date else {}
        if not isinstance(data, dict):
            data = {}
    except json.JSONDecodeError:
        data = {}
    data[symbol] = {"expiry": expiry, "date": date.today().isoformat()}
    strategy.last_entry_date = json.dumps(data)


def _resolve_current_expiry(broker, symbol: str, rules: dict) -> Optional[str]:
    """
    Lightweight pre-flight expiry resolution — used ONLY to decide
    whether this symbol's cycle has already been traded, before doing
    the full (order-placing) RuleBasedStrategy run. Deliberately
    duplicates the couple of calls RuleBasedStrategy._get_nearest_expiry()
    also makes internally, rather than sharing code, since this must run
    BEFORE any order can be placed (can't ask "did we already trade this
    cycle" by first running the very thing that would trade it).
    """
    from automate.utils.option_utils import find_nearest_expiry_by_type

    try:
        instrument_key = broker.resolve_instrument_key(symbol)
        expiries = broker.get_option_contracts(instrument_key)
        if not expiries:
            return None
        expiry_mode = (rules.get("expiry") or {}).get("mode", "WEEKLY")
        now = broker.get_current_time()
        return find_nearest_expiry_by_type(expiries, expiry_mode, reference_date=now.date() if now else None)
    except Exception as exc:
        log.warning("custom_strategy_scheduler: could not pre-resolve expiry for %s: %s", symbol, exc)
        return None


def _try_entry(db, strategy: CustomStrategy, broker) -> None:
    symbols = json.loads(strategy.symbols)
    rules = json.loads(strategy.rules_json)
    entry_rule = rules.get("entry") or {}

    if entry_rule.get("mode") == "AT_TIME":
        target = entry_rule.get("time")
        now_hhmm = datetime.now().strftime("%H:%M")
        if now_hhmm < target:
            return

    # Fetch all open positions for this strategy
    open_positions = db.query(CustomStrategyPosition).filter(
        CustomStrategyPosition.strategy_id == strategy.id,
        CustomStrategyPosition.status == "OPEN",
    ).all()

    for symbol in symbols:
        # Check if we already hold an open position for this symbol
        has_open = any(_is_leg_for_symbol(p.instrument_key, symbol) for p in open_positions)
        if has_open:
            continue

        # Gate on the CURRENT expiry cycle, not the calendar day — an
        # early exit (TP/SL hit) must not trigger a same-cycle
        # re-entry on the same soon-to-expire contract; wait for the
        # resolved nearest expiry to actually roll over to the next one.
        current_expiry = _resolve_current_expiry(broker, symbol, rules)
        if current_expiry is None:
            continue  # Couldn't resolve (broker hiccup) — retry next tick, don't mark anything.
        if _get_last_entered_expiry(strategy, symbol) == current_expiry:
            continue  # Already traded this cycle.

        try:
            rule_strategy = RuleBasedStrategy(
                broker=broker, audit=_audit, kill_switch=_kill_switch, rate_limiter=_rate_limiter,
                symbol=symbol, rules=rules, user_id=strategy.user_id,
            )
            result = rule_strategy.run()
        except Exception as exc:
            log.error("custom_strategy_scheduler: entry failed for strategy %s (%s) symbol %s: %s",
                      strategy.id, strategy.name, symbol, exc)
            notify(
                "custom_strategy",
                f"Entry failed for strategy \"{strategy.name}\" ({symbol}, {_mode_for_status(strategy.status)} mode): {exc}. "
                f"Will keep retrying every tick until the current expiry cycle rolls over.",
                level="warning",
                user_id=strategy.user_id,
            )
            # No _set_last_entered_expiry() here — nothing was placed, so
            # retry next tick rather than silently giving up on this cycle.
            continue

        if result.get("status") not in ("success", "dry_run"):
            continue
        _set_last_entered_expiry(strategy, symbol, result.get("expiry") or current_expiry)

        mode = _mode_for_status(strategy.status)
        for idx, leg in enumerate(result["legs"]):
            db.add(CustomStrategyPosition(
                strategy_id=strategy.id, leg_index=idx, mode=mode,
                instrument_key=leg["instrument_token"], instrument_type=leg["instrument_type"],
                option_type=leg["option_type"], strike=leg["strike"], expiry=leg["expiry"],
                transaction_type=leg["transaction_type"], quantity=leg["quantity"],
                entry_price=leg["entry_price"] or 0, order_id=leg.get("order_id"), status="OPEN",
            ))
        db.commit()
        log.info("custom_strategy_scheduler: entered strategy %s (%s) symbol %s — %d legs.",
                 strategy.id, strategy.name, symbol, len(result["legs"]))


def _combined_pnl_pct(legs: list[CustomStrategyPosition], now_prices: dict) -> Optional[float]:
    """
    NET P&L (of the combined premium at entry) — includes real Upstox
    transaction costs (brokerage/STT/exchange charges/GST/SEBI charges/
    stamp duty, see utils/costs.py) on both the original entry AND the
    hypothetical exit at `now_prices`, same as backtest/custom_engine.py
    computes for historical cycles. Slippage is already baked into
    leg.entry_price/now_prices themselves (PaperBroker/UpstoxBroker apply
    it at fill time) — this function only adds the fee layer backtest
    already had and paper/live previously didn't, so the two modes are
    now comparable apples-to-apples.

    Used both for the live TP/SL trigger check (so a "+40% profit"
    target is evaluated net of real costs, not a false-positive gross
    number) and for the final stored paper_return_pct/live_return_pct.
    """
    from automate.utils.costs import calculate_options_transaction_cost_breakdown

    pnl_amount = 0.0
    denom = 0.0
    for leg in legs:
        now = now_prices.get(leg.instrument_key)
        if now is None:
            return None
        entry = float(leg.entry_price)
        qty = leg.quantity
        sign = -1 if leg.transaction_type == "SELL" else 1
        pnl_amount += (entry - now) * qty * (-sign)
        denom += entry * qty

        exit_transaction_type = "BUY" if leg.transaction_type == "SELL" else "SELL"
        entry_costs = calculate_options_transaction_cost_breakdown(entry, qty, leg.transaction_type)
        exit_costs = calculate_options_transaction_cost_breakdown(now, qty, exit_transaction_type)
        pnl_amount -= entry_costs.get("total", 0) + exit_costs.get("total", 0)
    return (pnl_amount / denom * 100.0) if denom > 0 else 0.0


def _try_exit(db, strategy: CustomStrategy, broker) -> None:
    legs = db.query(CustomStrategyPosition).filter(
        CustomStrategyPosition.strategy_id == strategy.id,
        CustomStrategyPosition.status == "OPEN",
    ).all()
    if not legs:
        return

    rules = json.loads(strategy.rules_json)
    exit_rule = rules.get("exit") or {}
    take_profit_pct = exit_rule.get("take_profit_pct")
    stop_loss_pct = exit_rule.get("stop_loss_pct")
    exit_time = exit_rule.get("exit_time")
    exit_days_before_expiry = exit_rule.get("exit_days_before_expiry", 0)

    # Group open legs by symbol
    symbols = json.loads(strategy.symbols)
    legs_by_symbol = defaultdict(list)
    for leg in legs:
        leg_symbol = None
        for s in symbols:
            if _is_leg_for_symbol(leg.instrument_key, s):
                leg_symbol = s
                break
        if leg_symbol:
            legs_by_symbol[leg_symbol].append(leg)
        else:
            legs_by_symbol["UNKNOWN"].append(leg)

    for symbol, symbol_legs in legs_by_symbol.items():
        if symbol == "UNKNOWN":
            continue

        tokens = [leg.instrument_key for leg in symbol_legs]
        now_prices = broker.get_ltp_batch(tokens)
        pnl_pct = _combined_pnl_pct(symbol_legs, now_prices)

        trigger = check_exit_trigger(pnl_pct, take_profit_pct, stop_loss_pct) if pnl_pct is not None else None
        if trigger is None and exit_time and datetime.now().strftime("%H:%M") >= exit_time:
            trigger = "TIME_EXIT"
        if trigger is None and exit_days_before_expiry:
            expiries = [dtime_or_none(leg.expiry) for leg in symbol_legs if leg.expiry]
            if expiries and any(is_within_pre_expiry_buffer(date.today(), exp, exit_days_before_expiry) for exp in expiries):
                trigger = "EXPIRY"
        
        if trigger is None:
            continue

        for leg in symbol_legs:
            opposite = broker.place_buy_order if leg.transaction_type == "SELL" else broker.place_sell_order
            try:
                _rate_limiter.acquire()
                exit_order_id = opposite(
                    instrument_token=leg.instrument_key, quantity=leg.quantity, order_type="MARKET",
                    tag=f"CUSTOM_EXIT_{strategy.id}_{leg.leg_index}"[:20],
                )
            except Exception as exc:
                log.critical(
                    "custom_strategy_scheduler: FAILED to square off leg %s for strategy %s: %s — MANUAL INTERVENTION REQUIRED.",
                    leg.instrument_key, strategy.id, exc,
                )
                notify(
                    "custom_strategy",
                    f"MANUAL INTERVENTION REQUIRED — failed to exit \"{strategy.name}\" leg {leg.instrument_key} "
                    f"({trigger} triggered): {exc}. A position may still be open on your real account.",
                    user_id=strategy.user_id,
                )
                continue
            leg.status = "CLOSED"
            leg.exit_price = now_prices.get(leg.instrument_key)
            leg.exit_order_id = exit_order_id
            leg.exit_reason = trigger
            leg.closed_at = datetime.now()

        final_pct = _combined_pnl_pct(symbol_legs, now_prices) or 0.0
        if strategy.status == "LIVE":
            strategy.live_return_pct = round(final_pct, 4)
        else:
            strategy.paper_return_pct = round(final_pct, 4)
        db.commit()
        log.info(
            "custom_strategy_scheduler: exited strategy %s (%s) symbol %s | trigger=%s | pnl_pct=%.2f",
            strategy.id, strategy.name, symbol, trigger, final_pct,
        )


def dtime_or_none(expiry_str: Optional[str]):
    if not expiry_str:
        return None
    try:
        return date.fromisoformat(expiry_str)
    except ValueError:
        return None


async def custom_strategy_scheduler() -> None:
    """Persistent background task — see module docstring. Never raises; logs and keeps ticking."""
    log.info("custom_strategy_scheduler: started.")
    while True:
        try:
            market_open = _market_is_open_now()
            if market_open:
                brokers = _get_brokers()
                if brokers is not None:
                    db = SessionLocal()
                    try:
                        strategies = db.query(CustomStrategy).filter(
                            CustomStrategy.status.in_(["PAPER_TRADING", "LIVE"])
                        ).all()
                        for strategy in strategies:
                            if not strategy.rules_json:
                                continue
                            broker = brokers[_mode_for_status(strategy.status)]
                            try:
                                _try_exit(db, strategy, broker)
                                _try_entry(db, strategy, broker)
                            except Exception as exc:
                                log.error("custom_strategy_scheduler: tick failed for strategy %s: %s", strategy.id, exc, exc_info=True)
                    finally:
                        db.close()
        except Exception as exc:
            log.error("custom_strategy_scheduler: tick-level failure: %s", exc, exc_info=True)

        await asyncio.sleep(_TICK_SEC_OPEN if market_open else _TICK_SEC_CLOSED)
