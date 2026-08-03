"""
run_strategy.py — Main Entry Point for the trading bot.

This is the script you schedule via cron on your Ubuntu server (or, more
simply, that run_daemon.py calls internally on its own schedule).

There are exactly three modes anywhere in this system: paper, live, and
backtest (a separate subsystem — see automate.backtest). No fourth
"dry-run" gate layered on top. Paper vs live is chosen per strategy, not
here or on the command line — it's each strategy's own MODE in config.py's
STRATEGY_CONFIGS entry (see config.TenPercentOTMStrangleConfig.MODE). A
'paper' strategy always trades through PaperBroker (real market data,
simulated fills, never real money); a 'live' strategy always places real
orders with your real Upstox account — live means live. This lets you run
several strategies at once at different stages — one proven and live,
another still being paper-tested — without them interfering with each
other.

Usage:
    # Runs config.RunConfig.ACTIVE_STRATEGIES (.env ACTIVE_STRATEGIES), each in its own configured MODE
    python3 -m automate.cli.run_strategy

    # Run only specific strategies this time, regardless of ACTIVE_STRATEGIES
    python3 -m automate.cli.run_strategy --strategies ten_percent_otm_strangle

Cron example (runs at 09:20 IST = 03:50 UTC, Mon–Fri):
    50 3 * * 1-5 cd /var/www/html/automate && python3 -m automate.cli.run_strategy >> logs/cron.log 2>&1

Adding a strategy does NOT make it run automatically: strategies/registry.py
is shared with backtesting, so a strategy can exist there (and be
backtested) without ever going live — it only runs here once its name is
added to config.RunConfig.ACTIVE_STRATEGIES (or passed via --strategies).

SEBI Compliance:
    - Displays mandatory risk disclaimer on startup.
    - Validates market hours before any order is placed.
    - Activates kill switch on any unhandled exception.
    - Writes complete audit trail to logs/audit_trail.log.
"""

import argparse
import logging
import sys
from datetime import date

from automate.broker.broker_factory import BrokerFactory
from automate.compliance.sebi_rules import (
    AuditTrail,
    KillSwitch,
    OrderRateLimiter,
    init_market_calendar,
    print_risk_disclaimer,
)

# ── Project-root imports ────────────────────────────────────────────────────
from automate.config import STRATEGY_CONFIGS, LogConfig, RunConfig, UpstoxConfig
from automate.strategies.registry import STRATEGIES
from automate.utils.logger import setup_logger
from automate.utils.notify import notify
from automate.utils.position_tracker import has_open_position, record_open_position
from automate.utils.strategy_overrides import get_effective_config
from automate.utils.telegram_alert import alert_trade_opened

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Automated Strategy Runner (Upstox)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 -m automate.cli.run_strategy
  python3 -m automate.cli.run_strategy --strategies ten_percent_otm_strangle
        """,
    )
    parser.add_argument(
        "--strategies",
        default=None,
        help=(
            "Comma-separated strategy names to run this time, overriding "
            "config.RunConfig.ACTIVE_STRATEGIES (.env ACTIVE_STRATEGIES). "
            f"Available: {', '.join(STRATEGIES)}"
        ),
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    args = parse_args()

    # ── Step 1: Setup logging ────────────────────────────────────────────
    # Configure the ROOT logger so every module's `get_logger(__name__)`
    # (which has no handlers of its own and propagates by default) actually
    # reaches the console + logs/strategy.log, instead of being silently
    # dropped or leaking to stderr via Python's logging.lastResort handler.
    setup_logger(
        name="",
        level=LogConfig.LEVEL,
        log_file=LogConfig.FILE,
    )
    log = logging.getLogger("strategy_runner")

    # ── Step 2: SEBI mandatory risk disclaimer ───────────────────────────
    print_risk_disclaimer()

    # ── Step 3: Resolve which strategies to run ───────────────────────────
    selected_strategies = (
        [s.strip() for s in args.strategies.split(",") if s.strip()]
        if args.strategies else RunConfig.ACTIVE_STRATEGIES
    )
    unknown = [s for s in selected_strategies if s not in STRATEGIES or s not in STRATEGY_CONFIGS]
    if unknown:
        log.critical(
            "Unknown strategy name(s) %s. Available: %s. "
            "(Every entry needs both a strategies/registry.py class AND a config.STRATEGY_CONFIGS entry.)",
            unknown, list(STRATEGIES),
        )
        return 1
    if not selected_strategies:
        log.critical("No strategies selected — set ACTIVE_STRATEGIES in .env or pass --strategies.")
        return 1
    bad_mode = [s for s in selected_strategies if getattr(STRATEGY_CONFIGS[s], "MODE", None) not in ("paper", "live")]
    if bad_mode:
        log.critical("Strategy config(s) %s have an invalid/missing MODE — must be 'paper' or 'live'.", bad_mode)
        return 1

    # ── Step 4: Validate Upstox credentials ───────────────────────────────
    log.info("Strategies: %s", selected_strategies)
    UpstoxConfig.validate()

    # ── Step 5: Download NSE holidays + freeze quantities ─────────────────────────
    # Pass Upstox access token as fallback for the holiday API.
    # This is optional — the NSE public API works without auth.
    upstox_token_for_fallback = UpstoxConfig.ACCESS_TOKEN or ""
    log.info("Refreshing NSE market calendar (holidays + freeze quantities) ...")
    init_market_calendar(
        force=False,               # Use today's cache if available
        access_token=upstox_token_for_fallback,
    )

    # ── Step 6: Initialise SEBI compliance components ─────────────────────────
    kill_switch   = KillSwitch()
    rate_limiter  = OrderRateLimiter(max_per_second=10)
    audit         = AuditTrail(audit_log_path="logs/audit_trail.log")

    # ── Step 7: Create paper + live brokers (one real Upstox connection, shared) ─
    try:
        brokers = BrokerFactory.create_mode_brokers()
    except (ValueError, RuntimeError) as exc:
        log.critical("Failed to initialise Upstox broker: %s", exc)
        return 1

    # ── Step 8: Create and run each selected strategy, for each of its symbols ──
    overall_success = run_entries(brokers, audit, kill_switch, rate_limiter, selected_strategies, log)

    # ── Step 9: Exit code ────────────────────────────────────────────────
    if overall_success:
        log.info("All strategy runs completed successfully.")
        return 0
    else:
        log.error("One or more strategy runs failed.")
        return 1


def run_entries(
    brokers: dict, audit: AuditTrail, kill_switch: KillSwitch, rate_limiter: OrderRateLimiter,
    selected_strategies: list, log: logging.Logger,
) -> bool:
    """
    Attempt entry ONCE for every selected strategy x symbol combination.
    `brokers` is {'paper': PaperBroker, 'live': <real broker>} (see
    BrokerFactory.create_mode_brokers) — each strategy picks its own
    broker by its own config MODE, so 'paper' and 'live' strategies can
    run side by side in the same call.

    Extracted out of main() so run_daemon.py can call this same logic on
    its own internal schedule instead of needing a separate cron-timed
    process — see that script.

    NOTE: this shared kwarg call matches TenPercentOTMStrangle's
    constructor — the only strategy implemented today. A future strategy
    with a genuinely different constructor shape (extra/different params)
    would need per-strategy kwarg handling here, not one shared call —
    same caveat as backtest/engine.py's _execute_strategy(). Any keys in
    cfg.EXTRA_KWARGS are passed through too, so this loop stays generic
    across strategies with slightly different constructors.

    Returns: True if every attempted entry succeeded (or dry-ran), False
    if any failed.
    """
    overall_success = True

    for strategy_name in selected_strategies:
        strategy_cls = STRATEGIES[strategy_name]
        # Effective config = config.py's STRATEGY_CONFIGS defaults with any
        # runtime override (e.g. a MODE flip from the control-panel API)
        # layered on top — see utils/strategy_overrides.py. With no
        # override saved, this is identical to STRATEGY_CONFIGS[strategy_name].
        cfg = get_effective_config(strategy_name)
        broker = brokers[cfg.MODE]

        for symbol in cfg.SYMBOLS:
            if has_open_position(strategy_name, symbol):
                log.info(
                    "Skipping %s/%s — already has an open position (one cycle at a time; "
                    "it'll be re-eligible for entry the day after that position closes).",
                    strategy_name, symbol,
                )
                continue

            log.info("=" * 60)
            log.info("Starting %s for symbol: %s (mode=%s)", strategy_cls.__name__, symbol, cfg.MODE)
            log.info("=" * 60)

            try:
                strategy = strategy_cls(
                    broker=broker,
                    audit=audit,
                    kill_switch=kill_switch,
                    rate_limiter=rate_limiter,
                    symbol=symbol,
                    num_lots=cfg.NUM_LOTS,
                    # strike_step deliberately NOT passed — resolved
                    # dynamically inside the strategy from the broker's
                    # real instrument master (see TenPercentOTMStrangle.
                    # __init__()), same reasoning as lot size.
                    product=cfg.PRODUCT,
                    **getattr(cfg, "EXTRA_KWARGS", {}),
                )
                result = strategy.run()

                if result.get("status") not in ("success", "dry_run"):
                    log.error("Strategy run failed for %s/%s. Check logs for details.", strategy_name, symbol)
                    overall_success = False
                elif result.get("status") == "success":
                    # Every REAL (non-dry-run) fill is recorded, regardless
                    # of whether SL/TP is configured — run_position_monitor.py
                    # (or run_daemon.py's internal monitoring tick) needs to
                    # know this position exists even with SL/TP disabled, so
                    # it can still force-close it on/near expiry (physical
                    # settlement risk for stock options — see
                    # run_position_monitor.py's module docstring). Entering
                    # here does NOT itself monitor the position.
                    position_id = record_open_position(
                        strategy_name=strategy_name, mode=cfg.MODE, symbol=symbol,
                        entry_date=date.today().isoformat(),
                        expiry=result["expiry"],
                        call_token=result["call_token"], call_strike=result["call_strike"],
                        call_entry_price=result["call_entry_price"], call_order_id=result["call_order_id"],
                        put_token=result["put_token"], put_strike=result["put_strike"],
                        put_entry_price=result["put_entry_price"], put_order_id=result["put_order_id"],
                        quantity=result["quantity"], product=result["product"],
                        take_profit_pct=result["take_profit_pct"], stop_loss_pct=result["stop_loss_pct"],
                        exit_days_before_expiry=result["exit_days_before_expiry"],
                    )
                    log.info(
                        "Recorded open position #%d for %s/%s (mode=%s) | expiry=%s | TP=%s%% SL=%s%% | "
                        "exit >=%dd before expiry — will be watched (for SL/TP if set, and always for expiry).",
                        position_id, strategy_name, symbol, cfg.MODE, result["expiry"],
                        result["take_profit_pct"], result["stop_loss_pct"], result["exit_days_before_expiry"],
                    )
                    alert_trade_opened(
                        strategy_name, cfg.MODE, symbol,
                        f"CE {result['call_strike']}@{result['call_entry_price']:.2f} | "
                        f"PE {result['put_strike']}@{result['put_entry_price']:.2f} | "
                        f"qty={result['quantity']} | expiry={result['expiry']}",
                    )

            except Exception as exc:
                log.error("Strategy configuration/execution error for %s/%s: %s", strategy_name, symbol, exc)
                notify(f"{strategy_name}/{symbol}", f"Config/execution error before strategy could run: {exc}")
                overall_success = False

    return overall_success


if __name__ == "__main__":
    sys.exit(main())
