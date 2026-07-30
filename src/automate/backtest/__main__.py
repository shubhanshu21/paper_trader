"""
backtest/__main__.py — ONE simple command to backtest a strategy: symbol
name, type (index/stock), date range. That's it.

    python3 -m automate.backtest --symbol RELIANCE --type stock --from 2026-07-20 --to 2026-07-27
    python3 -m automate.backtest --symbol NIFTY --type index --from 2015-01-01 --to 2020-12-31

It automatically routes to whichever backend can actually serve the
requested range:

  - RECENT range (--from within the last ~30 days) -> real minute-candle
    data from Upstox: downloads it (scripts/download_real_history.py) and
    runs a real single-trade simulation (backtest/engine.py) with real
    fills, slippage, and P&L for that one trade.

  - OLDER range -> the bhavcopy database (dataset/fno_bhavcopy.db):
    simulates the strategy across every real historical monthly expiry
    cycle in that window (backtest/historical_engine.py) and reports
    aggregate win-rate/P&L statistics, not a single trade.

These are genuinely different kinds of results (one trade vs. many years
of cycles) because they come from genuinely different data (live minute
candles vs. daily EOD settlement) — this picks the right one for you
rather than silently pretending they're interchangeable.

KNOWN GAP: as of when this was written, live data covers roughly the last
30 days, and the bhavcopy database covers ~2000 to ~Aug 2020 plus (once
scripts/fill_bhavcopy_gap.py has been run) NSE's own archive from ~Sep
2020 to a few days ago. If you request a range this repo genuinely has no
data for, the underlying engine will report zero results/rows rather than
fabricate anything — check its output.
"""
import argparse
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

# src/automate/backtest/__main__.py -> up 4 levels to the repo root (where
# logs/, cache/, dataset/, scripts/ actually live) — subprocess calls below
# need this as their cwd. No sys.path hack needed: `automate` is installed
# editable (pip install -e .), so package imports just resolve normally.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
from automate.strategies.registry import STRATEGIES  # Single source of truth — see strategies/registry.py.

LIVE_DATA_MAX_DAYS = 30  # Upstox's real 1-minute history limit (verified empirically).
AVAILABLE_STRATEGIES = list(STRATEGIES)


def run(cmd: list) -> int:
    print(f"$ {' '.join(cmd)}", file=sys.stderr)
    return subprocess.call(cmd, cwd=str(REPO_ROOT))


def _pct_or_none(value: str):
    """argparse type: a percentage, or 'none'/'off'/'disabled' to turn the threshold off entirely."""
    if value.strip().lower() in ("none", "off", "disabled"):
        return None
    return float(value)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--symbol", required=True, help="e.g. RELIANCE, NIFTY, BANKNIFTY")
    parser.add_argument("--strategy", choices=AVAILABLE_STRATEGIES, default=AVAILABLE_STRATEGIES[0],
                         help="Which strategy to backtest. Only one is implemented today.")
    parser.add_argument("--type", choices=["index", "stock"], default=None,
                         help="Defaults to 'index' for NIFTY/BANKNIFTY/FINNIFTY/MIDCPNIFTY, else 'stock'.")
    parser.add_argument("--from", dest="from_date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--to", dest="to_date", required=True, help="YYYY-MM-DD")
    parser.add_argument(
        "--strike-step", type=float, default=None,
        help="Override the strike interval. Default: resolved dynamically from today's real "
             "instrument master (or the download manifest, for the recent-data path) — no "
             "hardcoded guess.",
    )
    parser.add_argument("--num-lots", type=int, default=1)
    parser.add_argument(
        "--take-profit-pct", type=_pct_or_none, default=None,
        help="Exit early once this %% of premium collected is captured. Default: None = always "
             "hold to expiry — opt-in only, validate against real data first (see README). Only "
             "applies to the older-date (historical) path — no effect on recent-date backtests.",
    )
    parser.add_argument(
        "--stop-loss-pct", type=_pct_or_none, default=None,
        help="Exit early once loss reaches this %% of premium collected. Default: None = always "
             "hold to expiry — opt-in only, validate against real data first (see README). Only "
             "applies to the older-date (historical) path — no effect on recent-date backtests.",
    )
    args = parser.parse_args()

    symbol = args.symbol.upper()
    index_symbols = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"}
    contract_type = args.type or ("index" if symbol in index_symbols else "stock")
    # Deliberately NOT defaulted to a hardcoded number here — None is passed
    # straight through so the underlying engine resolves it dynamically
    # from the real instrument master (or the download manifest, for the
    # recent-data path), same as live/paper trading. A previous version of
    # this wrapper computed a fallback (50/20) unconditionally, which
    # silently overrode that dynamic resolution every time — verified live
    # that this was actually happening (RELIANCE backtests were using the
    # stale hardcoded 20 instead of the real 10).
    strike_step = args.strike_step

    from_d = datetime.strptime(args.from_date, "%Y-%m-%d").date()
    today = date.today()

    if from_d >= today - timedelta(days=LIVE_DATA_MAX_DAYS):
        note = " (Note: --take-profit-pct/--stop-loss-pct don't apply on this path — see README.)" if (args.take_profit_pct is not None or args.stop_loss_pct is not None) else ""
        print(
            f"Checking recent dates ({args.from_date} to {args.to_date}) — testing 1 trade with real minute-by-minute data.{note}",
            file=sys.stderr,
        )
        days = max((today - from_d).days, 1)
        rc = run([sys.executable, "scripts/download_real_history.py", "--symbol", symbol, "--days", str(days)])
        if rc != 0:
            print("[backtest] Download failed — see errors above (e.g. expired Upstox token: python3 -m auth.upstox_auth).", file=sys.stderr)
            return rc
        cmd = [sys.executable, "-m", "automate.backtest.engine", "--symbol", symbol, "--strategy", args.strategy, "--num-lots", str(args.num_lots)]
        if strike_step is not None:
            cmd += ["--strike-step", str(strike_step)]
        return run(cmd)

    else:
        print(
            f"Checking older dates ({args.from_date} to {args.to_date}) — testing every monthly trade in this period.",
            file=sys.stderr,
        )
        cmd = [
            sys.executable, "-m", "automate.backtest.historical_engine",
            "--symbol", symbol, "--strategy", args.strategy, "--type", contract_type,
            "--num-lots", str(args.num_lots),
            "--from-date", args.from_date, "--to-date", args.to_date,
            "--take-profit-pct", "none" if args.take_profit_pct is None else str(args.take_profit_pct),
            "--stop-loss-pct", "none" if args.stop_loss_pct is None else str(args.stop_loss_pct),
        ]
        if strike_step is not None:
            cmd += ["--strike-step", str(strike_step)]
        return run(cmd)


if __name__ == "__main__":
    sys.exit(main())
