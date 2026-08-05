"""
run_position_monitor.py — Watches EVERY open position recorded by
run_strategy.py (regardless of whether stop_loss_pct/take_profit_pct is
configured) and exits any that have hit their stop-loss/take-profit
threshold, OR are within their own pre-expiry exit buffer.

Each position is exited through the SAME mode ('paper' or 'live') it was
entered with — recorded on the position row at entry time (see
utils/position_tracker.py), not re-derived from the strategy's current
config, so a later MODE change in config.py can't misroute an already
-open position's exit. mode='paper' always simulates; mode='live' always
places a real exit order — no separate dry-run flag on top of that.

This is a SEPARATE cron job from run_strategy.py's once-a-day entry —
entering a position does NOT, by itself, monitor it. Schedule this
script to run every few minutes during market hours:

    */5 9-15 * * 1-5 cd /path/to/automate && .venv/bin/python -m cli.run_position_monitor >> logs/position_monitor_cron.log 2>&1

Exit trigger math (utils.option_utils.strangle_pnl_pct/check_exit_trigger)
is the same used by backtest/historical_engine.py's day-by-day walk, so a
threshold that was validated in a backtest means the same thing here.

EXPIRY SAFETY NET: every open position (even with SL/TP disabled) is force
-closed once it enters its OWN recorded pre-expiry exit buffer
(`exit_days_before_expiry`, recorded at entry time — see
utils/position_tracker.py) — regardless of strategy, symbol, or expiry
cadence (weekly/monthly), since this reads each position's own `expiry`
field rather than assuming anything about which strategy created it. This
is deliberately NEVER as late as expiry day itself: besides the immediate
gamma/liquidity risk of the last day, STOCK options are compulsorily
PHYSICALLY settled in India (unlike index options, which are cash-settled)
— an ITM stock leg left open past expiry can trigger real share
delivery/receipt obligations far larger than the options margin the
position was actually using. Nothing else in this codebase does this
check, so this script must actually run in the days before expiry for it
to matter — the buffer only helps if something is actually watching for it.
"""
import logging
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from broker.broker_factory import BrokerFactory
from compliance.sebi_rules import (
    AuditTrail,
    OrderRateLimiter,
    assert_market_is_open,
    init_market_calendar,
)
from config import LogConfig, UpstoxConfig
from utils.logger import setup_logger
from utils.option_utils import (
    check_exit_trigger,
    is_within_pre_expiry_buffer,
    strangle_pnl_pct,
)
from utils.position_tracker import close_position, get_open_positions, get_position
from utils.telegram_alert import alert_manual_intervention, alert_trade_closed

_EXIT_MAX_ATTEMPTS = 3
_EXIT_RETRY_DELAY_SEC = 2.0


def _close_leg(
    broker, rate_limiter: OrderRateLimiter, audit: AuditTrail,
    symbol: str, token: str, option_type: str, quantity: int, product: str, tag_prefix: str,
    log: logging.Logger, user_id: int | None = None,
) -> str | None:
    """Buy back one leg, retrying like the live strategy's own auto-unwind (see TenPercentOTMStrangle._unwind_filled_legs)."""
    for attempt in range(1, _EXIT_MAX_ATTEMPTS + 1):
        try:
            rate_limiter.acquire()
            order_id = broker.place_buy_order(
                instrument_token=token, quantity=quantity, product=product,
                order_type="MARKET", tag=(f"{tag_prefix}_{option_type}")[:20],
                user_id=user_id,
            )
            audit.record(
                event_type="POSITION_EXIT", symbol=symbol, instrument_token=token,
                option_type=option_type, strike=0, quantity=quantity,
                order_id=order_id or "FAILED", status="CLOSED", note=f"attempt={attempt}",
            )
            return order_id
        except Exception as exc:
            log.error(
                "Exit attempt %d/%d failed for %s %s leg: %s",
                attempt, _EXIT_MAX_ATTEMPTS, symbol, option_type, exc,
            )
            if attempt < _EXIT_MAX_ATTEMPTS:
                time.sleep(_EXIT_RETRY_DELAY_SEC)
    return None


def _write_alert(position: dict, leg_status: dict, log: logging.Logger) -> None:
    """Same standalone-alert-file pattern as the strategy's own escalation — a partially-closed position must not depend on someone reading the log at the right moment."""
    alert_dir = Path("logs")
    alert_dir.mkdir(parents=True, exist_ok=True)
    alert_path = alert_dir / f"ALERT_MANUAL_INTERVENTION_{position['symbol']}_{datetime.now().strftime('%Y%m%dT%H%M%S')}.flag"
    try:
        alert_path.write_text(
            f"MANUAL INTERVENTION REQUIRED — position exit incomplete.\n"
            f"position_id={position['id']}\n"
            f"strategy={position['strategy_name']}\n"
            f"symbol={position['symbol']}\n"
            f"call_token={position['call_token']} status={leg_status.get('CE')}\n"
            f"put_token={position['put_token']} status={leg_status.get('PE')}\n"
            f"quantity={position['quantity']}\n"
            f"timestamp={datetime.now().isoformat()}\n"
            f"\nACTION: manually verify this position with your broker and square it "
            f"off if still open. The position remains OPEN in data/runtime/trading.db so this "
            f"script retries it next run — delete this file once resolved.\n"
        )
        log.critical("Alert file written: %s", alert_path)
    except OSError as write_exc:
        log.critical("Could not even write the alert file: %s", write_exc)
    alert_manual_intervention(
        f"position_id={position['id']} strategy={position['strategy_name']} "
        f"symbol={position['symbol']} CE={leg_status.get('CE')} PE={leg_status.get('PE')} — "
        f"exit incomplete, verify with broker manually. See {alert_path}."
    )


def monitor_once(brokers: dict, audit: AuditTrail, rate_limiter: OrderRateLimiter, log: logging.Logger) -> None:
    """
    Check every open position once: exit on SL/TP trigger or expiry.
    `brokers` is {'paper': PaperBroker, 'live': <real broker>} (see
    BrokerFactory.create_mode_brokers) — each position is exited through
    whichever broker matches its OWN recorded mode (pos["mode"], set at
    entry time), not the strategy's current config. mode='live' always
    places a real exit order when triggered; mode='paper' always simulates.

    Extracted out of main() so run_daemon.py can call this same logic on
    its own internal schedule instead of needing a separate cron-timed
    process — see that script. Assumes the caller has already confirmed
    the market is open (this does not check itself).
    """
    positions = get_open_positions()
    if not positions:
        log.info("No open positions to monitor.")
        return

    log.info("Monitoring %d open position(s).", len(positions))

    for pos in positions:
        broker = brokers[pos["mode"]]
        call_ltp = broker.get_ltp(pos["call_token"])
        put_ltp = broker.get_ltp(pos["put_token"])
        if call_ltp is None or put_ltp is None:
            log.warning(
                "Position #%d (%s/%s): could not fetch current LTPs — skipping this cycle, will retry next run.",
                pos["id"], pos["strategy_name"], pos["symbol"],
            )
            continue

        pnl_pct = strangle_pnl_pct(pos["call_entry_price"], pos["put_entry_price"], call_ltp, put_ltp)

        # Expiry safety net takes priority over SL/TP — a position must
        # never be left open into its own pre-expiry exit buffer, regardless
        # of whether SL/TP is even configured on it (see module docstring).
        today = date.today()
        expiry = date.fromisoformat(pos["expiry"])
        if is_within_pre_expiry_buffer(today, expiry, pos["exit_days_before_expiry"]):
            trigger = "EXPIRY"
        else:
            trigger = check_exit_trigger(pnl_pct, pos["take_profit_pct"], pos["stop_loss_pct"])

        log.info(
            "Position #%d %s/%s (mode=%s) | expiry=%s (exit by %s) | entry CE=%.2f PE=%.2f | now CE=%.2f PE=%.2f | pnl=%+.1f%% | TP=%s%% SL=%s%% | trigger=%s",
            pos["id"], pos["strategy_name"], pos["symbol"], pos["mode"], pos["expiry"],
            (expiry - timedelta(days=pos["exit_days_before_expiry"])).isoformat(),
            pos["call_entry_price"], pos["put_entry_price"], call_ltp, put_ltp,
            pnl_pct, pos["take_profit_pct"], pos["stop_loss_pct"], trigger or "none",
        )

        if trigger is None:
            continue

        log.critical(
            "[%s] Closing position #%d (%s/%s) — pnl=%+.1f%%",
            trigger, pos["id"], pos["strategy_name"], pos["symbol"], pnl_pct,
        )
        call_exit_id = _close_leg(broker, rate_limiter, audit, pos["symbol"], pos["call_token"], "CE", pos["quantity"], pos["product"], trigger, log, pos.get("user_id"))
        put_exit_id = _close_leg(broker, rate_limiter, audit, pos["symbol"], pos["put_token"], "PE", pos["quantity"], pos["product"], trigger, log, pos.get("user_id"))

        if call_exit_id is None or put_exit_id is None:
            log.critical(
                "Position #%d exit INCOMPLETE (CE=%s, PE=%s) — left OPEN for retry, alerting.",
                pos["id"], call_exit_id or "FAILED", put_exit_id or "FAILED",
            )
            _write_alert(pos, {"CE": call_exit_id or "FAILED", "PE": put_exit_id or "FAILED"}, log)
            continue

        close_position(
            pos["id"], exit_date=date.today().isoformat(), exit_reason=trigger,
            call_exit_price=call_ltp, put_exit_price=put_ltp,
            call_exit_order_id=call_exit_id, put_exit_order_id=put_exit_id,
        )
        log.info("Position #%d closed. reason=%s pnl=%+.1f%%", pos["id"], trigger, pnl_pct)
        alert_trade_closed(
            pos["strategy_name"], pos["mode"], pos["symbol"],
            f"reason={trigger} | pnl={pnl_pct:+.1f}% | CE exit={call_ltp:.2f} PE exit={put_ltp:.2f}",
        )


def close_position_manual(
    position_id: int, brokers: dict, audit: AuditTrail, rate_limiter: OrderRateLimiter, log: logging.Logger,
) -> dict:
    """
    Close one open position on demand (control-panel API's "Close position"
    action) — same leg-closing logic monitor_once() uses for a SL/TP/expiry
    trigger, just invoked directly for one position id instead of scanning
    every open position for a trigger. Raises ValueError if the position
    doesn't exist or isn't OPEN.
    """
    pos = get_position(position_id)
    if pos is None:
        raise ValueError(f"No position with id={position_id}")
    if pos["status"] != "OPEN":
        raise ValueError(f"Position #{position_id} is already {pos['status']}, not OPEN")

    broker = brokers[pos["mode"]]
    call_ltp = broker.get_ltp(pos["call_token"]) or pos["call_entry_price"]
    put_ltp = broker.get_ltp(pos["put_token"]) or pos["put_entry_price"]

    call_exit_id = _close_leg(broker, rate_limiter, audit, pos["symbol"], pos["call_token"], "CE", pos["quantity"], pos["product"], "MANUAL", log, pos.get("user_id"))
    put_exit_id = _close_leg(broker, rate_limiter, audit, pos["symbol"], pos["put_token"], "PE", pos["quantity"], pos["product"], "MANUAL", log, pos.get("user_id"))

    if call_exit_id is None or put_exit_id is None:
        _write_alert(pos, {"CE": call_exit_id or "FAILED", "PE": put_exit_id or "FAILED"}, log)
        raise RuntimeError(f"Position #{position_id} exit INCOMPLETE (CE={call_exit_id or 'FAILED'}, PE={put_exit_id or 'FAILED'}) — left OPEN, manual intervention alert written.")

    close_position(
        position_id, exit_date=date.today().isoformat(), exit_reason="MANUAL",
        call_exit_price=call_ltp, put_exit_price=put_ltp,
        call_exit_order_id=call_exit_id, put_exit_order_id=put_exit_id,
    )
    pnl_pct = strangle_pnl_pct(pos["call_entry_price"], pos["put_entry_price"], call_ltp, put_ltp)
    log.info("Position #%d manually closed. pnl=%+.1f%%", position_id, pnl_pct)
    alert_trade_closed(pos["strategy_name"], pos["mode"], pos["symbol"], f"reason=MANUAL | pnl={pnl_pct:+.1f}% | CE exit={call_ltp:.2f} PE exit={put_ltp:.2f}")
    return {"position_id": position_id, "pnl_pct": pnl_pct, "call_exit_price": call_ltp, "put_exit_price": put_ltp}


def main() -> int:
    setup_logger(name="", level=LogConfig.LEVEL, log_file=LogConfig.FILE)
    log = logging.getLogger("position_monitor")

    UpstoxConfig.validate()
    init_market_calendar(force=False, access_token=UpstoxConfig.ACCESS_TOKEN or "")

    try:
        assert_market_is_open()
    except RuntimeError as exc:
        log.info("Market closed — nothing to monitor right now: %s", exc)
        return 0

    brokers = BrokerFactory.create_mode_brokers()
    audit = AuditTrail(audit_log_path="logs/audit_trail.log")
    rate_limiter = OrderRateLimiter(max_per_second=10)

    monitor_once(brokers, audit, rate_limiter, log)
    return 0


if __name__ == "__main__":
    sys.exit(main())
