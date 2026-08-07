"""
backtest/synthetic_engine.py — replay-based backtester for signal-driven
engines this app has no other way to backtest (no real historical option
data — see backtest/synthetic_data_feed.py's own docstring for exactly
what's approximated and why, and read that before trusting these P&L
numbers as literal).

Drives the REAL strategy class (ZeroToHeroStrategy) through MockBroker +
SyntheticOptionDataFeed, minute-by-minute, over real historical
underlying candles (db.models.Index1MinCandle) — same "write the
strategy once, live trading AND every backtest use it automatically"
principle backtest/historical_engine.py's own docstring describes for
the bhavcopy-driven engine. The entry/exit STATE MACHINE below (TP1+
RUNNER split, SL/target/time exit, day-scoped re-entry gate) is a
replay-mode reimplementation of api/zero_to_hero_engine.py's live tick
logic — same rules, but tracking state in-memory instead of writing
CustomStrategyPosition rows, since a backtest run isn't a real position.

Usage:
    python3 -m backtest.synthetic_engine --symbol NIFTY --start 2024-01-01 --end 2024-12-31
    python3 -m backtest.synthetic_engine --symbol BANKNIFTY --start 2021-01-01 --end 2021-06-30 --lots 4
"""
import argparse
from datetime import date, datetime, timedelta

from sqlalchemy import text

from backtest.synthetic_data_feed import SyntheticOptionDataFeed
from broker.mock_broker import MockBroker
from compliance.sebi_rules import AuditTrail, KillSwitch, OrderRateLimiter
from db.engine import SessionLocal
from strategies.custom.zero_to_hero_schema import get_setting
from strategies.custom.zero_to_hero_strategy import ZeroToHeroStrategy
from utils.pnl import compute_basket_pnl

DEFAULT_RULES = {
    "lots": 2, "candle_interval_minutes": 15, "sl_buffer_points": 5,
    "max_pullback_candles": 2, "expiry_offset": 0, "exit_time": "15:15", "max_reentries": 1,
}


def _trading_days(session, symbol: str, start: date, end: date) -> list[date]:
    rows = session.execute(
        text("SELECT DISTINCT DATE(ts) FROM index_1min_candles WHERE symbol=:s AND ts >= :start AND ts <= :end ORDER BY 1"),
        {"s": symbol, "start": start.isoformat(), "end": f"{end.isoformat()} 23:59:59"},
    ).fetchall()
    return [r[0] for r in rows]


def _trading_minutes(session, symbol: str, day: date) -> list[datetime]:
    rows = session.execute(
        text("SELECT ts FROM index_1min_candles WHERE symbol=:s AND DATE(ts)=:d ORDER BY ts ASC"),
        {"s": symbol, "d": day.isoformat()},
    ).fetchall()
    return [r[0] for r in rows]


def _close_leg(leg: dict, ts: datetime, exit_price: float | None, reason: str) -> dict:
    entry, exit_ = leg["entry_price"] or 0.0, exit_price or 0.0
    sign = 1 if leg["transaction_type"] == "SELL" else -1
    gross_pnl = round((entry - exit_) * leg["qty"] * sign, 2)
    return {
        "entry_ts": leg["entry_ts"], "exit_ts": ts, "role": leg["role"], "bias": leg["bias"],
        "option_type": leg["option_type"], "strike": leg["strike"], "qty": leg["qty"],
        "entry_price": entry, "exit_price": exit_, "exit_reason": reason, "gross_pnl": gross_pnl,
    }


def _run_one_day(feed, broker, engine, minutes: list[datetime], rules: dict) -> list[dict]:
    exit_time = get_setting(rules, "exit_time")
    max_reentries = get_setting(rules, "max_reentries")
    lots = rules.get("lots", 2)

    entries_today = 0
    rr_hit_today = False
    last_trigger_ts = None
    open_legs: list[dict] = []
    trades: list[dict] = []

    for ts in minutes:
        feed.set_time(ts)
        now_hhmm = ts.strftime("%H:%M")

        if open_legs:
            spot = feed.get_ltp(feed.equity_key)
            time_exit = now_hhmm >= exit_time
            still_open = []
            for leg in open_legs:
                sl_hit = spot is not None and ((spot >= leg["sl"]) if leg["bias"] == "BEARISH" else (spot <= leg["sl"]))
                if sl_hit:
                    trades.append(_close_leg(leg, ts, feed.get_ltp(leg["instrument_token"]), "SL_HIT"))
                    if leg["role"] == "TP1":
                        rr_hit_today = False
                    continue
                if leg["role"] == "TP1" and not time_exit and spot is not None:
                    target_hit = (spot <= leg["target"]) if leg["bias"] == "BEARISH" else (spot >= leg["target"])
                    if target_hit:
                        trades.append(_close_leg(leg, ts, feed.get_ltp(leg["instrument_token"]), "PARTIAL_TP_1_1"))
                        rr_hit_today = True
                        continue
                if time_exit:
                    trades.append(_close_leg(leg, ts, feed.get_ltp(leg["instrument_token"]), "TIME_EXIT"))
                    continue
                still_open.append(leg)
            open_legs = still_open
            continue  # a position was open this tick — never also evaluate a fresh entry, same as the live engine

        if now_hhmm >= exit_time:
            continue
        if entries_today >= 1 + max_reentries:
            continue
        if entries_today >= 1 and rr_hit_today:
            continue

        try:
            signal = engine.evaluate_signal()
        except Exception:
            continue
        if signal is None or signal["signal"] == "NONE":
            continue
        if signal["trigger_timestamp"] == last_trigger_ts:
            continue

        tp1_lots, runner_lots = lots // 2, lots - lots // 2
        common = {"bias": signal["signal"], "sl": signal["sl_index_price"], "target": signal["target_index_price"], "entry_ts": ts}
        try:
            tp1_fill = engine.enter(signal["option_type"], tp1_lots * engine.real_lot_size)
            runner_fill = engine.enter(signal["option_type"], runner_lots * engine.real_lot_size)
        except Exception:
            continue

        for role, fill, qty in (("TP1", tp1_fill, tp1_lots * engine.real_lot_size), ("RUNNER", runner_fill, runner_lots * engine.real_lot_size)):
            open_legs.append({
                **common, "role": role, "qty": qty, "entry_price": fill["entry_price"],
                "instrument_token": fill["instrument_token"], "option_type": fill["option_type"],
                "strike": fill["strike"], "transaction_type": "BUY",
            })
        entries_today += 1
        rr_hit_today = False
        last_trigger_ts = signal["trigger_timestamp"]

    if open_legs:
        feed.set_time(minutes[-1])
        spot = feed.get_ltp(feed.equity_key)
        for leg in open_legs:
            trades.append(_close_leg(leg, minutes[-1], feed.get_ltp(leg["instrument_token"]) or spot, "EOD_FORCE_CLOSE"))

    return trades


def run_backtest(symbol: str, start: date, end: date, rules: dict | None = None) -> dict:
    rules = {**DEFAULT_RULES, **(rules or {})}
    session = SessionLocal()
    all_trades: list[dict] = []
    try:
        days = _trading_days(session, symbol, start, end)

        # ONE feed/broker/strategy for the whole run, preloaded ONCE —
        # see SyntheticOptionDataFeed.preload()'s own docstring on why a
        # per-day (or worse, per-tick) DB round-trip made this crawl in
        # practice. Extra lookback buffer covers both PDH/PDL (1 session)
        # and realized-vol's own trailing window.
        feed = SyntheticOptionDataFeed(session, symbol)
        lookback_days = max(30, feed.vol_lookback_days * 2 + 10)
        preload_start = datetime.combine(start, datetime.min.time()) - timedelta(days=lookback_days)
        preload_end = datetime.combine(end, datetime.min.time().replace(hour=23, minute=59))
        feed.preload(preload_start, preload_end)
        broker = MockBroker(data_feed=feed)
        engine = ZeroToHeroStrategy(
            broker=broker, audit=AuditTrail(audit_log_path="logs/synthetic_backtest_audit.log"),
            kill_switch=KillSwitch(), rate_limiter=OrderRateLimiter(), symbol=symbol, rules=rules, user_id=None,
        )

        for day in days:
            minutes = _trading_minutes(session, symbol, day)
            if not minutes:
                continue
            try:
                all_trades.extend(_run_one_day(feed, broker, engine, minutes, rules))
            except Exception as exc:
                print(f"  ! {day} failed: {exc}")
    finally:
        session.close()

    total_pnl = sum(t["gross_pnl"] for t in all_trades)
    wins = sum(1 for t in all_trades if t["gross_pnl"] > 0)
    return {
        "symbol": symbol, "start": start.isoformat(), "end": end.isoformat(),
        "days_scanned": len(days),
        "trades": all_trades, "trade_count": len(all_trades),
        "total_gross_pnl": round(total_pnl, 2),
        "win_rate_pct": round(100 * wins / len(all_trades), 1) if all_trades else 0.0,
        "avg_pnl_per_leg": round(total_pnl / len(all_trades), 2) if all_trades else 0.0,
    }


def _group_into_cycles(trades: list[dict], symbol: str, rates: dict | None) -> list[dict]:
    """
    Group flat per-leg trade records (TP1 + RUNNER, sharing one entry_ts)
    into ONE "cycle" dict per entry — the same shape backtest/
    custom_engine.py's CustomRuleBacktestEngine already produces
    (entry_date/exit_date/net_pnl/pnl_pct_of_premium/won/legs/...), so
    this can feed the SAME storage (CustomBacktestRun) and stats pipeline
    (utils.backtest_stats.compute_backtest_stats) the bhavcopy-driven
    engine already uses — one shared result shape, two different data
    sources behind it, matching this app's "N strategy_types, one shared
    dispatch" pattern (engine_registry.py, strategy_scheduler.py) rather
    than inventing a second, UI-incompatible report format.

    net_pnl here is CHARGES-AWARE (via utils.pnl.compute_basket_pnl, the
    same real-cost formula every other P&L surface in this app uses) —
    the flat trades list only carries gross_pnl.
    """
    by_entry: dict[datetime, list[dict]] = {}
    for t in trades:
        by_entry.setdefault(t["entry_ts"], []).append(t)

    cycles = []
    for entry_ts, legs in sorted(by_entry.items()):
        pnl_result = compute_basket_pnl([
            {"entry_price": leg["entry_price"], "exit_price": leg["exit_price"], "quantity": leg["qty"],
             "transaction_type": "BUY", "instrument_type": "OPTION"}
            for leg in legs
        ], rates)
        total_premium_paid = sum(leg["entry_price"] * leg["qty"] for leg in legs)
        exit_dates = [leg["exit_ts"] for leg in legs]
        exit_reasons = {leg["exit_reason"] for leg in legs}
        cycles.append({
            "entry_date": entry_ts.date().isoformat(),
            "expiry": None,  # not tracked per-cycle here (synthetic contracts use a heuristic weekly calendar, not a real listed expiry — see SyntheticOptionDataFeed's own docstring)
            "exit_date": max(exit_dates).date().isoformat(),
            "exit_reason": next(iter(exit_reasons)) if len(exit_reasons) == 1 else "MIXED",
            "spot_at_entry": None,
            "symbol": symbol,
            "legs": [
                {"instrument_type": "OPTION", "option_type": leg["option_type"], "strike": leg["strike"],
                 "transaction_type": "BUY", "quantity": leg["qty"], "entry_price": leg["entry_price"],
                 "exit_date": leg["exit_ts"].date().isoformat(), "exit_reason": leg["exit_reason"],
                 "exit_price": leg["exit_price"]}
                for leg in legs
            ],
            "gross_pnl": pnl_result["gross_pnl"],
            "charges": round(pnl_result["gross_pnl"] - pnl_result["net_pnl"], 2),
            "net_pnl": pnl_result["net_pnl"],
            "pnl_pct_of_premium": round(100 * pnl_result["net_pnl"] / total_premium_paid, 2) if total_premium_paid else 0.0,
            "won": pnl_result["net_pnl"] > 0,
            "liquid": True,  # synthetic pricing has no real order-book concept to judge liquidity from
        })
    return cycles


def run_zero_to_hero_backtest(
    symbols: list[str], rules: dict, instrument_type: str,
    from_date: str | None, to_date: str | None,
    on_progress=None, charge_rates: dict | None = None,
) -> tuple[list[dict], dict, dict]:
    """
    Drop-in replacement for api/routes_custom_strategies.py's
    _run_backtest_symbols(), same signature/return shape
    (cycles, per_symbol, skipped_symbols) — the dispatch in that route
    picks this OR the bhavcopy engine automatically based on
    strategy_type (see strategies.custom.engine_registry.get_engine),
    same "one shared entry point, N interchangeable engines behind it"
    pattern strategy_scheduler.py already uses for live ticking.
    """
    end = date.fromisoformat(to_date) if to_date else date.today()
    start = date.fromisoformat(from_date) if from_date else (end - timedelta(days=365))

    all_cycles: list[dict] = []
    per_symbol: dict[str, dict] = {}
    skipped_symbols: dict[str, str] = {}

    for symbol in symbols:
        symbol = symbol.upper()
        if symbol not in ("NIFTY", "BANKNIFTY"):
            skipped_symbols[symbol] = (
                "The synthetic intraday backtest only has real underlying candle history for "
                "NIFTY and BANKNIFTY (see db.models.Index1MinCandle) — no data for this symbol."
            )
            continue

        result = run_backtest(symbol, start, end, rules=rules)
        cycles = _group_into_cycles(result["trades"], symbol, charge_rates)
        if not cycles:
            skipped_symbols[symbol] = "No historical cycles produced a valid simulated trade in this date range."
            continue

        all_cycles.extend(cycles)
        wins = sum(1 for c in cycles if c["won"])
        per_symbol[symbol] = {
            "cycles_tested": len(cycles),
            "avg_return_pct": round(sum(c["pnl_pct_of_premium"] for c in cycles) / len(cycles), 2),
            "win_rate_pct": round(wins / len(cycles) * 100.0, 2),
        }
        if on_progress:
            on_progress(symbol, 100, 100)

    all_cycles.sort(key=lambda c: c["entry_date"])
    return all_cycles, per_symbol, skipped_symbols


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="NIFTY", choices=["NIFTY", "BANKNIFTY"])
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD")
    parser.add_argument("--lots", type=int, default=2)
    args = parser.parse_args()

    result = run_backtest(
        args.symbol, date.fromisoformat(args.start), date.fromisoformat(args.end),
        rules={"lots": args.lots},
    )
    print(f"\n=== {result['symbol']} {result['start']} .. {result['end']} ===")
    print(f"Legs closed:   {result['trade_count']}")
    print(f"Win rate:      {result['win_rate_pct']}%")
    print(f"Total P&L:     {result['total_gross_pnl']}")
    print(f"Avg P&L/leg:   {result['avg_pnl_per_leg']}")
    for t in result["trades"][:20]:
        print(f"  {t['entry_ts']} -> {t['exit_ts']} | {t['role']:7s} {t['option_type']} {t['strike']} | "
              f"{t['entry_price']:.2f} -> {t['exit_price']:.2f} | {t['exit_reason']:16s} | P&L {t['gross_pnl']:.2f}")
