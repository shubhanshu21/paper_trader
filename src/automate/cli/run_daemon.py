"""
run_daemon.py — ONE persistent process that handles both strategy entry
and position monitoring on its own internal schedule. No cron timing
flags needed — market-hours awareness, "enter once a day", and "check
positions every few minutes" all live here in Python, not in crontab
syntax. Start it once; it runs continuously and only ACTS during real
NSE market hours (checked internally, every tick, via
utils/market_calendar.py — the same live-fetched holiday/session logic
every other entry point already uses).

There are exactly three modes anywhere in this system: paper, live, and
backtest — no fourth "dry-run" gate. Paper vs live is NOT a daemon-level
setting — it's each strategy's own MODE in config.py's STRATEGY_CONFIGS
entry. This daemon builds both a paper broker and a live broker once, and
run_entries()/monitor_once() route each strategy/position to whichever
one its own MODE says, so paper-tested and already-live strategies can
run side by side.

Usage:
    python3 -m automate.cli.run_daemon                            # everything from .env — this is the normal cron invocation
    python3 -m automate.cli.run_daemon --strategies ten_percent_otm_strangle

Each tick:
  - Send a once-a-day Telegram heartbeat ("still running", open position
    count, market status) regardless of market status — trade/error
    alerts only fire on activity, so a hung/crashed daemon on an
    otherwise-quiet day would look identical to "all fine" without this.
  - If the market is closed right now (weekend, holiday, or outside
    09:15-15:30 IST), sleep a long interval and check again later.
  - If the market is open:
      - Attempt strategy entry ONCE per calendar day — tracked via a
        marker file (logs/.entry_done_<date>) so a restart mid-day won't
        double-enter. Same entry logic as run_strategy.py
        (run_strategy.run_entries()).
      - Check every open position for stop-loss/take-profit/expiry
        triggers and exit as needed — same logic as
        run_position_monitor.py (run_position_monitor.monitor_once()).
      - Sleep a SHORT interval before the next tick.

Cron becomes ONE line — just keep this process alive, nothing about
timing, nothing about paper/live:
    @reboot cd /path/to/automate && nohup .venv/bin/python -m automate.cli.run_daemon >> logs/daemon.log 2>&1 &

@reboot only fires at actual system boot, so also start it manually once
after setup (same command, without the leading "@reboot"). For a more
robust restart-on-crash setup than a bare @reboot line, run it under
systemd (ExecStart=this same command, Restart=always) instead — cron
itself has no process supervision.

SEBI Compliance: identical checks to run_strategy.py / run_position_monitor.py
(kill switch, price bands, rate limiter, audit trail) — this script is
just a different way of SCHEDULING the same, already-compliant logic. One
difference worth knowing: a KillSwitch activated by an unhandled exception
would normally stay tripped forever within a single process's lifetime —
wrong for a daemon meant to run for weeks, so a FRESH KillSwitch is
created before each day's entry attempt specifically, isolating one bad
day from blocking every future one (see main()).
"""
import argparse
import logging
import os
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import date
from pathlib import Path
from typing import Optional

from automate.config import RunConfig, LogConfig, STRATEGY_CONFIGS, UpstoxConfig
from automate.utils.logger import setup_logger
from automate.utils.telegram_alert import alert_heartbeat
from automate.utils.notify import notify
from automate.utils.position_tracker import get_open_positions
from automate.broker.broker_factory import BrokerFactory
from automate.compliance.sebi_rules import (
    AuditTrail,
    KillSwitch,
    OrderRateLimiter,
    assert_market_is_open,
    init_market_calendar,
    print_risk_disclaimer,
)
from automate.strategies.registry import STRATEGIES
from automate.cli.run_strategy import run_entries
from automate.cli.run_position_monitor import monitor_once
from automate.auth.upstox_auto_login import ensure_fresh_upstox_token
from automate.utils.strategy_overrides import get_effective_config

# How long to sleep between ticks — short while the market's open (stay
# responsive to SL/TP/expiry), long while it's closed (nothing to do,
# no point spinning).
_MARKET_OPEN_POLL_SEC = 60
_MARKET_CLOSED_POLL_SEC = 300

# The Selenium-driven login (ensure_fresh_upstox_token, when a refresh is
# actually needed) normally finishes in ~20-30s, but has been observed
# taking up to ~3.5 minutes when Upstox's login page or the underlying
# WebDriver connection is slow — a transient network retry, not a hang.
# This bounds it so a genuinely stuck browser session can never block the
# daemon's whole loop (entries + monitoring for every other position)
# indefinitely; on timeout we just skip this attempt and keep the
# existing token, same as any other refresh failure.
_TOKEN_REFRESH_TIMEOUT_SEC = 300


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--strategies", default=None,
                         help=f"Comma-separated strategy names, overriding ACTIVE_STRATEGIES. Available: {', '.join(STRATEGIES)}")
    return parser.parse_args()


def _market_is_open_now() -> bool:
    """Non-raising wrapper around the same live market-hours check every other entry point uses."""
    try:
        assert_market_is_open()
        return True
    except RuntimeError:
        return False


def _entry_marker_path() -> Path:
    return Path("logs") / f".entry_done_{date.today().isoformat()}"


def _token_refresh_marker_path() -> Path:
    return Path("logs") / f".token_refresh_done_{date.today().isoformat()}"


def _heartbeat_marker_path() -> Path:
    return Path("logs") / f".heartbeat_done_{date.today().isoformat()}"


# PID file so an external process (the control-panel API's daemon-control
# routes) can tell whether this daemon is actually running and stop it
# cleanly (SIGTERM) without needing its own separate process-supervision
# logic — same file regardless of how the daemon was started (cron
# @reboot, a human's terminal, or the API's subprocess.Popen).
PID_FILE = Path("logs") / "daemon.pid"


def _ensure_fresh_upstox_token_bounded(log: logging.Logger) -> Optional[str]:
    """
    Same contract as ensure_fresh_upstox_token(), but never blocks the
    caller past _TOKEN_REFRESH_TIMEOUT_SEC. The underlying Selenium call
    keeps running in its background thread either way (it cleans up its
    own browser session in a `finally`, see upstox_auto_login.py) — this
    just stops WAITING on it and moves on, so a slow/stuck login attempt
    can never freeze entries or position monitoring for everyone else.
    """
    # NOT a `with` block deliberately — ThreadPoolExecutor's context-manager
    # exit calls shutdown(wait=True), which would block on the very thread
    # we're trying to stop waiting on. shutdown(wait=False) here lets that
    # thread keep running and clean up on its own, without us waiting on it.
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(ensure_fresh_upstox_token)
    try:
        return future.result(timeout=_TOKEN_REFRESH_TIMEOUT_SEC)
    except FutureTimeoutError:
        log.critical(
            "Upstox token refresh exceeded %ds — abandoning this attempt, keeping existing token "
            "(the login itself keeps running in the background and will clean up on its own).",
            _TOKEN_REFRESH_TIMEOUT_SEC,
        )
        notify(
            "Upstox auto-login",
            f"Token refresh took longer than {_TOKEN_REFRESH_TIMEOUT_SEC}s — abandoned for this "
            f"cycle. If this keeps happening, check manually with `python3 -m automate.auth.upstox_auto_login`.",
        )
        return None
    finally:
        executor.shutdown(wait=False)


def _raise_keyboard_interrupt(signum, frame) -> None:
    raise KeyboardInterrupt()


def main() -> int:
    # A SIGTERM (e.g. from the control-panel API's "stop daemon" button, or
    # `kill <pid>`) is turned into the same KeyboardInterrupt the run loop
    # below already handles cleanly — no separate shutdown path needed.
    signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)

    args = parse_args()
    setup_logger(name="", level=LogConfig.LEVEL, log_file=LogConfig.FILE)
    log = logging.getLogger("daemon")

    print_risk_disclaimer()

    selected_strategies = (
        [s.strip() for s in args.strategies.split(",") if s.strip()]
        if args.strategies else RunConfig.ACTIVE_STRATEGIES
    )
    unknown = [s for s in selected_strategies if s not in STRATEGIES or s not in STRATEGY_CONFIGS]
    if unknown:
        log.critical("Unknown strategy name(s) %s. Available: %s.", unknown, list(STRATEGIES))
        return 1
    if not selected_strategies:
        log.critical("No strategies selected — set ACTIVE_STRATEGIES in .env or pass --strategies.")
        return 1
    modes = {s: getattr(STRATEGY_CONFIGS[s], "MODE", None) for s in selected_strategies}
    bad_mode = [s for s, m in modes.items() if m not in ("paper", "live")]
    if bad_mode:
        log.critical("Strategy config(s) %s have an invalid/missing MODE — must be 'paper' or 'live'.", bad_mode)
        return 1

    log.info("Daemon starting | strategies=%s", modes)
    # No-ops if UPSTOX_USERNAME/PIN/TOTP_SECRET aren't set (auto-login opt
    # -in) — see auth/upstox_auto_login.py. Runs BEFORE broker construction
    # so a fresh daemon start always has a valid token, without a human
    # having run `python3 -m automate.auth.upstox_auth` first. Bounded so a
    # stuck login can't prevent the daemon from starting at all.
    _ensure_fresh_upstox_token_bounded(log)
    UpstoxConfig.validate()
    init_market_calendar(force=False, access_token=UpstoxConfig.ACCESS_TOKEN or "")

    try:
        brokers = BrokerFactory.create_mode_brokers()
    except (ValueError, RuntimeError) as exc:
        log.critical("Failed to initialise Upstox broker: %s", exc)
        return 1

    audit = AuditTrail(audit_log_path="logs/audit_trail.log")
    rate_limiter = OrderRateLimiter(max_per_second=10)

    log.info("Daemon running — Ctrl+C to stop.")
    try:
        while True:
            # Once a day, regardless of market status — trade/error alerts
            # only fire on activity, so a hung/crashed daemon on an
            # otherwise-quiet day (market closed, or open with nothing to
            # do) would otherwise look identical to "all fine" from the
            # outside. Deliberately NOT inside the market-open branch: a
            # weekend/holiday should still confirm the process is alive.
            heartbeat_marker = _heartbeat_marker_path()
            if not heartbeat_marker.exists():
                open_count = len(get_open_positions())
                market_open_now = _market_is_open_now()
                # Recomputed fresh (not the startup-time `modes` dict) so a
                # MODE flip made via the control-panel API while the daemon
                # is running shows up correctly, without needing a restart.
                current_modes = {s: get_effective_config(s).MODE for s in selected_strategies}
                log.info("Sending daily heartbeat | open_positions=%d | market_open=%s", open_count, market_open_now)
                alert_heartbeat(current_modes, open_count, market_open_now)
                heartbeat_marker.parent.mkdir(parents=True, exist_ok=True)
                heartbeat_marker.touch()

            if _market_is_open_now():
                token_marker = _token_refresh_marker_path()
                if not token_marker.exists():
                    old_token = UpstoxConfig.ACCESS_TOKEN
                    new_token = _ensure_fresh_upstox_token_bounded(log)
                    if new_token and new_token != old_token:
                        log.info("Upstox token changed — rebuilding brokers with the fresh token.")
                        brokers = BrokerFactory.create_mode_brokers()
                    token_marker.parent.mkdir(parents=True, exist_ok=True)
                    token_marker.touch()

                marker = _entry_marker_path()
                if not marker.exists():
                    log.info("Market open, no entry attempted yet today — running entries.")
                    # Fresh KillSwitch per day, not shared across the
                    # daemon's whole (potentially weeks-long) lifetime —
                    # see module docstring.
                    kill_switch = KillSwitch()
                    run_entries(brokers, audit, kill_switch, rate_limiter, selected_strategies, log)
                    marker.parent.mkdir(parents=True, exist_ok=True)
                    marker.touch()

                monitor_once(brokers, audit, rate_limiter, log)
                time.sleep(_MARKET_OPEN_POLL_SEC)
            else:
                log.debug("Market closed — sleeping %ds.", _MARKET_CLOSED_POLL_SEC)
                time.sleep(_MARKET_CLOSED_POLL_SEC)
    except KeyboardInterrupt:
        log.info("Daemon stopped (Ctrl+C or SIGTERM).")
        return 0


def _run_with_pid_file() -> int:
    """
    Wraps main() so PID_FILE always exists exactly while this process is
    alive, regardless of which of main()'s several return points/exceptions
    ends it — the API's daemon-status check just needs "does this pid
    exist and is it running", not to hook every exit path inside main()
    itself.
    """
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))
    try:
        return main()
    finally:
        PID_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(_run_with_pid_file())
