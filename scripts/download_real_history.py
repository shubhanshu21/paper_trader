"""
scripts/download_real_history.py — Download REAL historical candles for backtesting.

Simple usage — just plain symbol names, everything else is auto-resolved:

    python3 scripts/download_real_history.py --symbol RELIANCE
    python3 scripts/download_real_history.py --symbol NIFTY
    python3 scripts/download_real_history.py --symbols RELIANCE,TCS,NIFTY,BANKNIFTY
    python3 scripts/download_real_history.py --all

No manual ISIN/instrument_key lookup needed: the equity/index key is
resolved from today's Upstox instrument master (utils/instrument_cache.py),
and the CE/PE legs are auto-picked using the exact same ±10% strike-band
logic TenPercentOTMStrangle uses live right now (utils/option_utils.py) —
so the option contracts you download are the ones the strategy would
actually trade today.
"""
import argparse
import csv
import os
import sys
from datetime import date, timedelta

import upstox_client
from dotenv import load_dotenv
from upstox_client.rest import ApiException

from automate.config import TenPercentOTMStrangleConfig, UpstoxConfig
from automate.utils.logger import setup_logger

log = setup_logger("download_history", level="INFO")

_BAND_PCT = 0.10  # Matches strategies/stocks/ten_percent_otm_strangle.py

# Empirically verified Upstox History API v2 date-range limits per interval
# (exceeding these fails with HTTP 400 "UDAPI1148: Invalid date range").
# None = no practical limit hit (tested 1000+ days on 'day').
_INTERVAL_MAX_DAYS = {
    "1minute": 30,
    "30minute": 90,
    "day": None,
    "week": None,
}


def download_historical_data(instrument_key: str, interval: str, to_date: str, from_date: str) -> list[list]:
    """
    Downloads historical candle data using Upstox API.
    """
    UpstoxConfig.validate()

    configuration = upstox_client.Configuration()
    configuration.access_token = UpstoxConfig.ACCESS_TOKEN

    # Note: the installed upstox_client.ApiClient does not implement the
    # context-manager protocol (__enter__/__exit__), so it must be used
    # directly rather than via `with`.
    api_client = upstox_client.ApiClient(configuration)
    api = upstox_client.HistoryApi(api_client)
    try:
        log.info(f"Fetching {interval} data for {instrument_key} from {from_date} to {to_date}...")
        response = api.get_historical_candle_data1(
            instrument_key=instrument_key,
            interval=interval,
            to_date=to_date,
            from_date=from_date,
            api_version="2.0"
        )
        if response.data and response.data.candles:
            return response.data.candles
        return []
    except ApiException as e:
        log.error(f"Upstox API Error for {instrument_key}: {e.body}")
        return []
    except Exception as e:
        log.error(f"Error fetching historical data for {instrument_key}: {e}")
        return []


def save_to_csv(filename: str, candles: list[list]) -> None:
    """
    Saves the candle data to a CSV file.
    Format: timestamp, open, high, low, close, volume, open_interest
    """
    os.makedirs(os.path.dirname(filename), exist_ok=True)
    with open(filename, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['timestamp', 'open', 'high', 'low', 'close', 'volume', 'open_interest'])
        writer.writerows(candles)
    log.info(f"Saved {len(candles)} records to {filename}")


# ---------------------------------------------------------------------------
# Auto-resolution: symbol name -> instrument_key -> real CE/PE legs
# ---------------------------------------------------------------------------

def resolve_symbol_key(symbol: str) -> str:
    """Resolve a plain ticker ('RELIANCE' or 'NIFTY') to its Upstox instrument_key."""
    from automate.utils.instrument_cache import InstrumentCache
    cache = InstrumentCache(broker_name="upstox")
    key = cache.resolve_key(symbol.upper())
    if not key:
        log.error(
            "Could not resolve instrument_key for '%s' in today's Upstox "
            "instrument master (cache/upstox_instruments_*.csv). Check spelling, "
            "or run with a fresh cache if it's stale.", symbol,
        )
        sys.exit(1)
    return key


def resolve_atm_legs(
    symbol: str,
    equity_key: str,
    strike_step: float,
    broker,
) -> tuple[int, str, int, str, str, float]:
    """
    Auto-resolve the CE/PE strikes/tokens + expiry a live TenPercentOTMStrangle
    run right now would actually pick, using the exact same logic
    (utils.option_utils) against a real, live Upstox connection.

    Returns:
        (call_strike, call_token, put_strike, put_token, expiry, spot_price)
    """
    from automate.utils.option_utils import (
        calculate_strangle_strikes,
        find_instrument_token,
        find_nearest_monthly_expiry,
    )

    spot = broker.get_ltp(equity_key)
    if spot is None or spot <= 0:
        log.error("Could not fetch current LTP for %s (%s).", symbol, equity_key)
        sys.exit(1)

    call_strike, put_strike = calculate_strangle_strikes(spot, _BAND_PCT, strike_step)

    expiries = broker.get_option_contracts(equity_key)
    expiry = find_nearest_monthly_expiry(expiries)
    if not expiry:
        log.error("No usable expiry found for %s.", symbol)
        sys.exit(1)

    chain = broker.get_option_chain(equity_key, expiry)
    call_token = find_instrument_token(chain, call_strike, "CE")
    put_token = find_instrument_token(chain, put_strike, "PE")
    if not call_token or not put_token:
        log.error(
            "Could not resolve CE/PE tokens for %s at strikes %d/%d expiry %s.",
            symbol, call_strike, put_strike, expiry,
        )
        sys.exit(1)

    log.info(
        "%s | spot=₹%.2f | CE=%d(%s) | PE=%d(%s) | expiry=%s",
        symbol, spot, call_strike, call_token, put_strike, put_token, expiry,
    )
    return call_strike, call_token, put_strike, put_token, expiry, spot


def download_symbol(
    symbol: str,
    days: int,
    interval: str,
    strike_step: float | None,
    spot_only: bool,
    broker=None,
) -> dict | None:
    """
    Download real spot candles (and, unless spot_only, real CE/PE candles
    for the strikes the strategy would pick right now) for one symbol.

    Returns a dict of everything needed to run backtest/engine.py against
    the downloaded data, or None if spot_only.
    """
    equity_key = resolve_symbol_key(symbol)
    end_date = date.today().isoformat()
    start_date = (date.today() - timedelta(days=days)).isoformat()

    log.info("=== %s (%s) ===", symbol, equity_key)
    spot_candles = download_historical_data(equity_key, interval, end_date, start_date)
    spot_path = f"data/historical/{symbol}_spot_{start_date}_to_{end_date}.csv"
    save_to_csv(spot_path, spot_candles)

    if spot_only:
        return None

    call_strike, call_token, put_strike, put_token, expiry, _spot = resolve_atm_legs(
        symbol, equity_key, strike_step, broker,
    )

    ce_candles = download_historical_data(call_token, interval, end_date, start_date)
    ce_path = f"data/historical/{symbol}_CE_{start_date}_to_{end_date}.csv"
    save_to_csv(ce_path, ce_candles)

    pe_candles = download_historical_data(put_token, interval, end_date, start_date)
    pe_path = f"data/historical/{symbol}_PE_{start_date}_to_{end_date}.csv"
    save_to_csv(pe_path, pe_candles)

    info = {
        "symbol": symbol, "equity_key": equity_key, "spot_path": spot_path,
        "expiry": expiry, "call_strike": call_strike, "call_token": call_token, "call_path": ce_path,
        "put_strike": put_strike, "put_token": put_token, "put_path": pe_path,
        # Saved so backtest/engine.py's strategy recomputes the SAME
        # strikes from spot at run time — it must match whatever was
        # actually used here to decide which CE/PE candles to download.
        "strike_step": strike_step,
    }
    write_manifest(info)
    return info


def write_manifest(info: dict) -> str:
    """
    Save everything backtest/engine.py needs for this symbol as
    data/historical/<symbol>_manifest.json, so it can be re-run later with
    just `python3 -m backtest.engine --symbol <symbol>` instead of
    re-typing every token/strike/path by hand.
    """
    import json
    path = f"data/historical/{info['symbol']}_manifest.json"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(info, f, indent=2)
    return path


def print_backtest_command(info: dict) -> None:
    log.info(
        "Ready to backtest %s → python3 -m backtest.engine --symbol %s "
        "(manifest saved — all strikes/tokens/paths are picked up automatically)",
        info["symbol"], info["symbol"],
    )


def main():
    load_dotenv()

    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--symbol", help="One plain symbol, e.g. RELIANCE or NIFTY.")
    group.add_argument("--symbols", help="Comma-separated symbols, e.g. RELIANCE,TCS,NIFTY.")
    group.add_argument(
        "--all", action="store_true",
        help="Download every symbol in config.TenPercentOTMStrangleConfig.KNOWN_STOCK_SYMBOLS "
             "plus KNOWN_INDEX_SYMBOLS (NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY).",
    )
    parser.add_argument(
        "--days", type=int, default=5,
        help="How many trailing days of candles to fetch. Upstox caps this per "
             "interval: 1minute=30 days, 30minute=90 days, day/week=effectively "
             "unlimited (verified empirically — exceeding it fails with HTTP 400 "
             "UDAPI1148 'Invalid date range').",
    )
    parser.add_argument("--interval", default="1minute")
    parser.add_argument(
        "--spot-only", action="store_true",
        help="Only download spot candles — skip auto-resolving/downloading CE/PE legs.",
    )
    args = parser.parse_args()

    max_days = _INTERVAL_MAX_DAYS.get(args.interval)
    if max_days is not None and args.days > max_days:
        log.warning(
            "--days %d exceeds Upstox's known limit for --interval %s (%d days). "
            "Clamping to %d days. For a longer historical window, use "
            "--interval day (loses intraday timing/slippage realism) instead.",
            args.days, args.interval, max_days, max_days,
        )
        args.days = max_days

    if args.symbol:
        symbols = [args.symbol.upper()]
    elif args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = list(TenPercentOTMStrangleConfig.KNOWN_STOCK_SYMBOLS) + list(TenPercentOTMStrangleConfig.KNOWN_INDEX_SYMBOLS)

    broker = None
    if not args.spot_only:
        UpstoxConfig.validate()
        from automate.broker.upstox_broker import UpstoxBroker
        broker = UpstoxBroker(access_token=UpstoxConfig.ACCESS_TOKEN, dry_run=True)

    def strike_step_for(sym: str) -> float | None:
        # Resolved live from the real instrument master — no hardcoded
        # table, same reasoning as lot size (see config.py). None here
        # (spot_only mode, or genuinely unresolvable) is fine: it's only
        # ever consumed by resolve_atm_legs(), which download_symbol()
        # doesn't call in spot_only mode.
        if broker is None:
            return None
        step = broker.get_strike_step(sym)
        if step is None:
            log.error(
                "Could not resolve a real strike step for '%s' from today's instrument master. "
                "Skipping — refusing to guess.", sym,
            )
        return step

    results = []
    failures = []
    for symbol in symbols:
        try:
            step = strike_step_for(symbol)
            if not args.spot_only and step is None:
                failures.append(symbol)
                continue
            info = download_symbol(
                symbol, args.days, args.interval, step, args.spot_only, broker,
            )
            if info:
                results.append(info)
        except SystemExit:
            failures.append(symbol)
            log.error("Skipping %s due to the error above.", symbol)

    log.info("=" * 60)
    log.info("Done: %d/%d symbols succeeded.", len(symbols) - len(failures), len(symbols))
    if failures:
        log.warning("Failed: %s", ", ".join(failures))
    for info in results:
        print_backtest_command(info)


if __name__ == "__main__":
    main()
