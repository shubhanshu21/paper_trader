"""
backtest/historical_engine.py — Runs the REAL strategy class against every
historical monthly expiry cycle found in the bhavcopy database
(dataset/fno_bhavcopy.db, built by scripts/import_bhavcopy_to_db.py).

This answers "what would this strategy's win rate / P&L distribution have
looked like historically" — not a single backtest run, but aggregate
statistics across ~15-20 years of real expiry cycles.

Unlike a previous design where a separate scripts/*.py file hand-wrote its
own copy of the strategy's strike-selection/entry logic in raw SQL (a
second implementation that could silently drift from the real strategy),
this engine drives the ACTUAL strategy class (e.g. TenPercentOTMStrangle)
through MockBroker + BhavcopyDataFeed — exactly like backtest/engine.py
already does for real intraday minute-candle data. Write a new strategy
once, and both live trading and every backtest use it automatically —
nothing to keep in sync by hand. Also run directly as a CLI:

    python3 -m automate.backtest.historical_engine --symbol NIFTY
    python3 -m automate.backtest.historical_engine --symbol BANKNIFTY --strike-step 100

Per-cycle isolation: BaseStrategy.run() permanently activates whatever
KillSwitch it's given on ANY unhandled exception (correct for live
trading — halt everything, require manual review). For a multi-cycle
historical run that would be wrong: one cycle's data gap (e.g. an illiquid
strike missing from a specific day's bhavcopy) must not silently blank out
every later cycle. So each cycle gets its own fresh KillSwitch; AuditTrail
and the rate limiter are shared across the whole run, like a real bot's
would be over its lifetime.

IMPORTANT — data quality caveats (read before trusting these numbers):
  - Source is DAILY EOD bhavcopy, not intraday ticks. Entry/exit prices are
    the day's CLOSE price, not a real achievable execution price at any
    specific time of day. There is no slippage model here (unlike
    backtest/engine.py) because there's no intraday price path to slip
    against.
  - We deliberately use `close`, NOT `settle_pr`, as the option price (see
    backtest/bhavcopy_data_feed.py's module docstring) — `settle_pr` is
    corrupted on the expiry date itself for stock/index options.
  - Zero-volume days can still report a theoretical/carried-forward `close`
    with NO real trade behind it — most common for exactly the ~10%-OTM
    strikes this strategy sells. Every leg's real `volume` (CONTRACTS) is
    checked, so results are split into "ALL cycles" vs "LIQUID-ONLY cycles"
    (every one of the 4 legs — entry CE/PE, exit CE/PE — had real volume
    that day). These two produce meaningfully different statistics; the ALL
    numbers are optimistic and should not be trusted at face value.
  - Transaction costs use TODAY's rates (utils/costs.py) applied
    retroactively — STT/exchange charges/GST have all changed multiple
    times since 2000, so cost estimates for older cycles are approximate,
    not historically accurate.
  - "Spot" at entry is the near-month FUTIDX/FUTSTK close — a futures-price
    proxy, not the literal index/equity spot level (bhavcopy has no
    cash-equity close, only F&O instruments).
  - Exit is the LAST available trade_date for that expiry in the data (a
    proxy for expiry settlement), not necessarily the true expiry day —
    the data can have gaps.
  - "Money Blocked (approx)" / "Money Needed to Trade This" is a ROUGH
    estimate of the margin your broker would hold to let you sell this
    strangle (spot x quantity x ~11% for index / ~18% for stock — common
    industry SPAN+Exposure ballparks). This is NOT your real broker margin
    figure — only your broker's live margin calculator knows that exactly.
  - --num-lots is how many REAL NSE lots to trade per leg — actual
    quantity used for P&L/costs is num_lots x the lot size resolved LIVE
    from today's cached Upstox instrument master, exactly like live trading
    (no hardcoded table). NSE revises lot sizes periodically (e.g.
    NIFTY/BANKNIFTY changed in Jan 2026) and this does not reconstruct the
    historical lot-size timeline — quantity is computed using TODAY's real
    lot size for every cycle, even old ones.
"""
import argparse
import statistics
from datetime import date, datetime, time as dtime
from typing import Dict, List, Optional
from sqlalchemy import text
from automate.db.engine import SessionLocal

from automate.backtest.bhavcopy_data_feed import BhavcopyDataFeed
from automate.broker.mock_broker import MockBroker
from automate.compliance.sebi_rules import AuditTrail, KillSwitch, OrderRateLimiter
from automate.strategies.registry import STRATEGIES
from automate.utils.costs import calculate_options_transaction_cost_breakdown, sum_breakdowns
from automate.utils.instrument_cache import InstrumentCache
from automate.utils.margin import estimate_margin_blocked
from automate.utils.logger import get_logger
from automate.utils.option_utils import check_exit_trigger, strangle_pnl_pct, is_within_pre_expiry_buffer

log = get_logger(__name__)

# A few minutes after the real 09:15 IST market open — matches when the
# live bot would actually be triggered, and satisfies the market-hours
# compliance gate for whatever date is being simulated.
_MARKET_OPEN = dtime(9, 20)

def _estimate_capital_needed(spot: float, quantity: int, option_instrument: str) -> float:
    """Rough estimate of money a broker blocks to sell this strangle. See utils/margin.py."""
    return estimate_margin_blocked(spot, quantity, is_index=(option_instrument == "OPTIDX"))


def _explain_result(net_pnl: float, ce_leg_pnl: float, pe_leg_pnl: float, call_strike: int, put_strike: int) -> str:
    """Plain-language reason this trade won or lost, based on which side (Call/Put) moved against us."""
    if net_pnl > 0:
        return "Won — stock stayed in the safe range, both options got cheaper"
    if ce_leg_pnl < 0 and pe_leg_pnl < 0:
        return "Lost — both sides got costlier (a big/volatile move)"
    if ce_leg_pnl <= pe_leg_pnl:
        return f"Lost — stock rose too close to/above the Call strike ({call_strike:.0f})"
    return f"Lost — stock fell too close to/below the Put strike ({put_strike:.0f})"


class HistoricalCycleEngine:
    """Simulates one broker + one strategy instance across every historical monthly expiry cycle in a date range."""

    def __init__(
        self,
        db_path: Optional[str],
        symbol: str,
        strategy: str = "ten_percent_otm_strangle",
        num_lots: int = 1,
        strike_step: Optional[float] = None,
        product: str = "NRML",
        option_instrument: str = "OPTSTK",
        future_instrument: str = "FUTSTK",
        audit_log_path: str = "logs/mock_audit_trail.log",
        strategy_kwargs: Optional[dict] = None,
    ) -> None:
        if strategy not in STRATEGIES:
            raise ValueError(f"Unknown strategy '{strategy}'. Available: {list(STRATEGIES)}")

        self.symbol = symbol.upper()
        self.strategy_cls = STRATEGIES[strategy]
        self.num_lots = num_lots
        self.strike_step = strike_step
        self.product = product
        self.option_instrument = option_instrument
        self.future_instrument = future_instrument
        # Extra per-strategy constructor kwargs beyond the shared ones above
        # — see config.STRATEGY_CONFIGS.EXTRA_KWARGS for the same pattern
        # used by run_strategy.py.
        self.strategy_kwargs = strategy_kwargs or {}

        self.session = SessionLocal()

        equity_key = InstrumentCache().resolve_key(self.symbol)
        if not equity_key:
            raise RuntimeError(
                f"Could not resolve an instrument key for '{self.symbol}' from today's cached "
                f"Upstox instrument master. Run `python3 scripts/download_real_history.py "
                f"--symbol {self.symbol}` first to populate/refresh the cache."
            )
        self.equity_key = equity_key

        self.feed = BhavcopyDataFeed(self.session, self.symbol, equity_key, option_instrument, future_instrument)
        # Real daily settle/close prices already ARE the achievable
        # execution price at this granularity — there's no intraday path
        # to slip against, so slippage is 0 here (unlike the intraday
        # backtest/engine.py, which does model real slippage).
        self.broker = MockBroker(data_feed=self.feed, slippage_pct=0.0)

        self.audit = AuditTrail(audit_log_path=audit_log_path)
        self.rate_limiter = OrderRateLimiter(max_per_second=10)

    def discover_cycles(self, from_date: Optional[str], to_date: Optional[str]) -> List[dict]:
        """
        Find every historical (entry_date, expiry, exit_date) in range —
        pure calendar/data lookup (which dates the bot would have run on),
        not strategy business logic (strike selection, entry decisions).
        """
        expiries = [
            r[0]
            for r in self.session.execute(
                text(
                    "SELECT DISTINCT expiry_dt FROM fno_bhavcopy WHERE symbol=:symbol AND instrument=:instrument "
                    "AND expiry_dt IS NOT NULL ORDER BY expiry_dt"
                ),
                {"symbol": self.symbol, "instrument": self.option_instrument},
            ).fetchall()
        ]
        fut_dates = [
            r[0]
            for r in self.session.execute(
                text(
                    "SELECT DISTINCT trade_date FROM fno_bhavcopy WHERE symbol=:symbol AND instrument=:instrument "
                    "ORDER BY trade_date"
                ),
                {"symbol": self.symbol, "instrument": self.future_instrument},
            ).fetchall()
        ]

        cycles = []
        prev_expiry = None
        for expiry in expiries:
            # Entry = first future trade date strictly after the previous
            # expiry (start of this contract's "current month" window).
            entry_date = fut_dates[0] if prev_expiry is None else next((d for d in fut_dates if d > prev_expiry), None)
            prev_expiry = expiry
            if entry_date is None or entry_date >= expiry:
                continue
            if (from_date and expiry < from_date) or (to_date and expiry > to_date):
                continue

            exit_row = self.session.execute(
                text(
                    "SELECT MAX(trade_date) FROM fno_bhavcopy WHERE symbol=:symbol AND instrument=:instrument "
                    "AND expiry_dt=:expiry"
                ),
                {"symbol": self.symbol, "instrument": self.option_instrument, "expiry": expiry},
            ).fetchone()
            exit_date = exit_row[0] if exit_row else None
            if not exit_date or exit_date <= entry_date:
                continue

            cycles.append({"entry_date": entry_date, "expiry": expiry, "exit_date": exit_date})
        return cycles

    def run(self, from_date: Optional[str] = None, to_date: Optional[str] = None) -> List[dict]:
        results = []
        try:
            for cycle in self.discover_cycles(from_date, to_date):
                row = self._run_one_cycle(cycle)
                if row is not None:
                    results.append(row)
        finally:
            self.session.close()
        return results

    def _run_one_cycle(self, cycle: dict) -> Optional[dict]:
        entry_date, expiry, exit_date = cycle["entry_date"], cycle["expiry"], cycle["exit_date"]

        self.feed.set_time(datetime.combine(date.fromisoformat(entry_date), _MARKET_OPEN))
        orders_before = len(self.broker.orders)

        strategy = self.strategy_cls(
            broker=self.broker,
            audit=self.audit,
            kill_switch=KillSwitch(),  # fresh per cycle — see module docstring.
            rate_limiter=self.rate_limiter,
            symbol=self.symbol,
            num_lots=self.num_lots,
            strike_step=self.strike_step,
            product=self.product,
            **self.strategy_kwargs,
        )
        result = strategy.run()

        if result.get("status") != "success":
            log.info(
                "Cycle entry=%s expiry=%s skipped: %s",
                entry_date, expiry, result.get("error", result.get("status")),
            )
            return None

        cycle_orders = self.broker.orders[orders_before:]
        sells = [o for o in cycle_orders if o["transaction_type"] == "SELL"]
        if len(sells) != 2:
            log.warning(
                "Cycle entry=%s expiry=%s: expected 2 SELL fills, got %d — skipping.",
                entry_date, expiry, len(sells),
            )
            return None

        # ── Stop-loss / take-profit / pre-expiry buffer: walk real daily
        # closes between entry and the original (held-to-expiry) exit
        # date, day by day, and exit on the FIRST day ANY trigger fires —
        # see utils.option_utils.check_exit_trigger()/
        # is_within_pre_expiry_buffer(). exit_days_before_expiry defaults
        # to 1 (matching TenPercentOTMStrangle's own default — see
        # config.py), so this walk runs for every cycle by default now,
        # not just when SL/TP is explicitly configured: the real strategy
        # never actually holds all the way to expiry, so a backtest that
        # did would silently stop matching real behavior.
        take_profit_pct = self.strategy_kwargs.get("take_profit_pct")
        stop_loss_pct = self.strategy_kwargs.get("stop_loss_pct")
        exit_days_before_expiry = self.strategy_kwargs.get("exit_days_before_expiry", 1)
        exit_reason = "EXPIRY"
        if take_profit_pct is not None or stop_loss_pct is not None or exit_days_before_expiry:
            call_token = next(s["instrument_token"] for s in sells if s["instrument_token"].endswith("|CE"))
            put_token = next(s["instrument_token"] for s in sells if s["instrument_token"].endswith("|PE"))
            call_entry_price = result["call_entry_price"]
            put_entry_price = result["put_entry_price"]
            expiry_date = date.fromisoformat(expiry)

            trading_days = [
                r[0]
                for r in self.session.execute(
                    text(
                        "SELECT DISTINCT trade_date FROM fno_bhavcopy WHERE symbol=:symbol AND instrument=:instrument AND "
                        "expiry_dt=:expiry AND trade_date > :entry_date AND trade_date <= :exit_date ORDER BY trade_date"
                    ),
                    {
                        "symbol": self.symbol,
                        "instrument": self.option_instrument,
                        "expiry": expiry,
                        "entry_date": entry_date,
                        "exit_date": exit_date,
                    },
                ).fetchall()
            ]

            for day in trading_days:
                self.feed.set_time(datetime.combine(date.fromisoformat(day), _MARKET_OPEN))
                ce_now = self.feed.get_ltp(call_token)
                pe_now = self.feed.get_ltp(put_token)
                if ce_now is None or pe_now is None:
                    continue  # no price that day (gap) — can't evaluate, keep holding.
                pnl_pct = strangle_pnl_pct(call_entry_price, put_entry_price, ce_now, pe_now)
                trigger = check_exit_trigger(pnl_pct, take_profit_pct, stop_loss_pct)
                if trigger is None and exit_days_before_expiry and is_within_pre_expiry_buffer(
                    date.fromisoformat(day), expiry_date, exit_days_before_expiry
                ):
                    trigger = "EXPIRY"
                if trigger:
                    exit_date = day
                    exit_reason = trigger
                    break

        # ── Mark both legs to market at exit, using the REAL entry fill
        # prices already recorded by the broker (with the strategy's own
        # real quantity) — no separate P&L formula from the strategy's own.
        self.feed.set_time(datetime.combine(date.fromisoformat(exit_date), _MARKET_OPEN))

        gross_pnl = 0.0
        net_pnl = 0.0
        leg_pnl: Dict[str, float] = {}
        leg_charges = []
        liquid = True
        for sell in sells:
            token = sell["instrument_token"]
            exit_price = self.feed.get_ltp(token)
            if exit_price is None:
                log.warning(
                    "Cycle entry=%s expiry=%s: no exit price for %s — skipping cycle.",
                    entry_date, expiry, token,
                )
                return None

            entry_costs = calculate_options_transaction_cost_breakdown(sell["execution_price"], sell["quantity"], "SELL")
            exit_costs = calculate_options_transaction_cost_breakdown(exit_price, sell["quantity"], "BUY")
            leg_charges.extend([entry_costs, exit_costs])
            leg_gross = (sell["execution_price"] - exit_price) * sell["quantity"]
            gross_pnl += leg_gross
            net_pnl += leg_gross - entry_costs["total"] - exit_costs["total"]
            leg_pnl["CE" if token.endswith("|CE") else "PE"] = leg_gross

            if self.feed.get_volume(token) <= 0:
                liquid = False
        # Entry-side liquidity: check the same two legs on the entry date.
        self.feed.set_time(datetime.combine(date.fromisoformat(entry_date), _MARKET_OPEN))
        for sell in sells:
            if self.feed.get_volume(sell["instrument_token"]) <= 0:
                liquid = False

        call_strike, put_strike = result["call_strike"], result["put_strike"]
        quantity = sells[0]["quantity"]
        reason = _explain_result(net_pnl, leg_pnl.get("CE", 0.0), leg_pnl.get("PE", 0.0), call_strike, put_strike)
        capital_needed = _estimate_capital_needed(result["spot_price"], quantity, self.option_instrument)

        return {
            "expiry": expiry, "entry_date": entry_date, "exit_date": exit_date,
            "spot": result["spot_price"], "call_strike": call_strike, "put_strike": put_strike,
            "gross_pnl": gross_pnl, "net_pnl": net_pnl, "liquid": liquid,
            "charges": sum_breakdowns(*leg_charges),
            "reason": reason, "capital_needed": capital_needed, "exit_reason": exit_reason,
        }


# ---------------------------------------------------------------------------
# CLI + plain-language report — Indian Rupee formatting, no jargon.
# ---------------------------------------------------------------------------

def _fmt_money(x: float) -> str:
    from automate.utils.money import format_inr
    return format_inr(x)


def _fmt_money_plain(x: float) -> str:
    from automate.utils.money import format_inr
    return format_inr(x, signed=False)


def compute_stats(results: List[dict]) -> dict:
    """Compute plain money numbers for one set of cycle results (or None fields if empty)."""
    if not results:
        return {"cycles": 0}
    net = [r["net_pnl"] for r in results]
    capital = [r["capital_needed"] for r in results]
    pct = [(n / c * 100) if c else 0.0 for n, c in zip(net, capital)]
    wins = sum(1 for x in net if x > 0)
    max_capital = max(capital)
    return {
        "cycles": len(results),
        "wins": wins,
        "win_rate": f"{wins} out of {len(results)} won ({100 * wins / len(results):.0f}%)",
        "total": _fmt_money(sum(net)),
        "average": _fmt_money(statistics.mean(net)),
        "best": _fmt_money(max(net)),
        "worst": _fmt_money(min(net)),
        "capital": _fmt_money_plain(max_capital),
        "average_pct": f"{statistics.mean(pct):+.2f}%",
        "total_pct": f"{(sum(net) / max_capital * 100) if max_capital else 0.0:+.2f}%",
    }


_EXIT_REASON_LABELS = {"EXPIRY": "Pre-Expiry Exit", "TAKE_PROFIT": "Take Profit", "STOP_LOSS": "Stop Loss"}


def print_trade_details(results: List[dict], show_exit_reason: bool = False) -> None:
    """Print one row per trade taken — entry, exit, strikes, and the result."""
    from automate.utils.table import render_stats_table

    headers = ["#", "Entry Date", "Exit Date", "Call Strike", "Put Strike", "Money Blocked (approx)", "Profit/Loss", "Return %"]
    if show_exit_reason:
        headers.append("Exit Reason")

    rows = []
    for i, r in enumerate(results, 1):
        pct = (r["net_pnl"] / r["capital_needed"] * 100) if r["capital_needed"] else 0.0
        row = [
            str(i),
            r["entry_date"],
            r["exit_date"],
            f"{r['call_strike']:.0f}",
            f"{r['put_strike']:.0f}",
            _fmt_money_plain(r["capital_needed"]),
            _fmt_money(r["net_pnl"]),
            f"{pct:+.2f}%",
        ]
        if show_exit_reason:
            row.append(_EXIT_REASON_LABELS.get(r["exit_reason"], r["exit_reason"]))
        rows.append(row)
    render_stats_table("Trades Taken", headers, rows, pnl_columns=[6, 7])


def print_summary_table(all_results: List[dict], liquid_results: List[dict], meta: dict) -> None:
    """Print a simple, plain-language money summary — no jargon, Indian Rupee formatting."""
    from automate.utils.table import render_stats_table, print_meta_line

    stats = compute_stats(all_results)

    title = f"{meta['symbol']} — Results for {meta['from_date'] or 'earliest'} to {meta['to_date'] or 'latest'}"

    if stats["cycles"] == 0:
        print(f"\n{title}\nNo trades found for this date range.")
        return

    rows = [
        ["Number of Trades", str(stats["cycles"])],
        ["Trades that Made Profit", stats["win_rate"]],
        ["Money Needed to Trade This (approx)", stats["capital"]],
        ["Total Profit / Loss", f"{stats['total']}  ({stats['total_pct']})"],
        ["Average per Trade", f"{stats['average']}  ({stats['average_pct']})"],
        ["Best Trade", stats["best"]],
        ["Worst Trade", stats["worst"]],
    ]
    render_stats_table(title, ["", "Amount"], rows)

    illiquid = stats["cycles"] - len(liquid_results)
    note = f"{illiquid} of these trades had low trading volume and may be hard to enter/exit in real life." if illiquid else None
    print_meta_line({
        "Stock/Index": meta["symbol"],
        "Lots per Trade": meta["num_lots"],
        **({"Note": note} if note else {}),
    })


def _pct_or_none(value: str) -> Optional[float]:
    """argparse type: a percentage, or 'none'/'off'/'disabled' to turn the threshold off entirely."""
    if value.strip().lower() in ("none", "off", "disabled"):
        return None
    return float(value)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--symbol", required=True, help="e.g. NIFTY, BANKNIFTY, or a stock symbol like RELIANCE")
    parser.add_argument("--strategy", choices=list(STRATEGIES), default=list(STRATEGIES)[0],
                         help="Which strategy to simulate. Only one is implemented today.")
    parser.add_argument("--type", choices=["index", "stock"], default="index",
                         help="index -> FUTIDX/OPTIDX rows, stock -> FUTSTK/OPTSTK rows")
    parser.add_argument("--db", default="dataset/fno_bhavcopy.db")
    parser.add_argument(
        "--strike-step", type=float, default=None,
        help="Override the strike interval (e.g. 20, 2.5). Default: None = resolved dynamically "
             "from today's real instrument master, same as live/paper trading (see "
             "TenPercentOTMStrangle.__init__()) — no hardcoded table, matches real listed strikes.",
    )
    parser.add_argument("--num-lots", type=int, default=1, help="How many real NSE lots per leg (quantity = num_lots x real lot size).")
    parser.add_argument("--from-date", default=None, help="YYYY-MM-DD, optional lower bound (by expiry date)")
    parser.add_argument("--to-date", default=None, help="YYYY-MM-DD, optional upper bound (by expiry date)")
    parser.add_argument(
        "--take-profit-pct", type=_pct_or_none, default=None,
        help="Exit early once this %% of premium collected is captured (e.g. 60 = +60%%). "
             "Default: None = disabled, only the pre-expiry buffer below controls exit timing. "
             "Opt-in only — a backtest over a longer window showed a tight take-profit can cut "
             "winners short and reduce total return; validate against real data first, see README.",
    )
    parser.add_argument(
        "--stop-loss-pct", type=_pct_or_none, default=None,
        help="Exit early once loss reaches this %% of premium collected (positive magnitude, "
             "e.g. 150 = -150%%/1.5x). Default: None = disabled, only the pre-expiry buffer below "
             "controls exit timing. Opt-in only — it caps worst-case loss but reduced total return "
             "over a longer real-data backtest by cutting recoveries short; validate first, see README.",
    )
    parser.add_argument(
        "--exit-days-before-expiry", type=int, default=1,
        help="Always exit at least this many days before expiry, regardless of SL/TP — matches "
             "TenPercentOTMStrangle's own real default (config.py's EXIT_DAYS_BEFORE_EXPIRY), so "
             "this backtest reflects what live/paper trading actually does. Pass 0 to see the "
             "original held-to-expiry behavior instead, for comparison.",
    )
    args = parser.parse_args()

    # Load once, upfront, before running any cycles — otherwise the
    # market-hours compliance check (called inside the strategy, once per
    # cycle) lazy-loads it on first use with a "was not called before use"
    # warning. One real network call for the whole multi-cycle run.
    from automate.compliance.sebi_rules import init_market_calendar
    init_market_calendar(force=False)

    future_instrument = "FUTIDX" if args.type == "index" else "FUTSTK"
    option_instrument = "OPTIDX" if args.type == "index" else "OPTSTK"

    strategy_kwargs = {"exit_days_before_expiry": args.exit_days_before_expiry}
    if args.take_profit_pct is not None:
        strategy_kwargs["take_profit_pct"] = args.take_profit_pct
    if args.stop_loss_pct is not None:
        strategy_kwargs["stop_loss_pct"] = args.stop_loss_pct

    engine = HistoricalCycleEngine(
        db_path=args.db, symbol=args.symbol, strategy=args.strategy,
        num_lots=args.num_lots, strike_step=args.strike_step,
        option_instrument=option_instrument, future_instrument=future_instrument,
        strategy_kwargs=strategy_kwargs,
    )
    results = engine.run(args.from_date, args.to_date)

    # Exit reason is always meaningful now (EXPIRY/TAKE_PROFIT/STOP_LOSS),
    # not just when SL/TP was explicitly set — show it unconditionally.
    print_trade_details(results, show_exit_reason=True)
    print_summary_table(
        results, [r for r in results if r["liquid"]],
        meta={
            "symbol": args.symbol, "type": args.type, "strategy": args.strategy,
            "num_lots": args.num_lots,
            "from_date": args.from_date, "to_date": args.to_date,
        },
    )


if __name__ == "__main__":
    main()
