"""
backtest/engine.py — Real Historical-Data Backtesting Engine

Steps the strategy through REAL historical candles (loaded via
DataFeed.load_from_csv(), sourced from scripts/download_real_history.py).
No synthetic prices are used anywhere in this path.
"""
import json
import os
from datetime import datetime

from automate.backtest.data_feed import DataFeed
from automate.broker.mock_broker import MockBroker
from automate.compliance.sebi_rules import AuditTrail, KillSwitch, OrderRateLimiter
from automate.strategies.registry import STRATEGIES
from automate.utils.costs import calculate_options_transaction_cost
from automate.utils.logger import get_logger

log = get_logger(__name__)


class BacktestEngine:
    def __init__(
        self,
        data_feed: DataFeed,
        symbol: str = "RELIANCE",
        strategy: str = "ten_percent_otm_strangle",
        num_lots: int = 1,
        strike_step: float | None = None,
        product: str = "NRML",
        slippage_pct: float = 0.001,
        strategy_kwargs: dict | None = None,
    ):
        if strategy not in STRATEGIES:
            raise ValueError(f"Unknown strategy '{strategy}'. Available: {list(STRATEGIES)}")
        self.data_feed = data_feed
        self.symbol = symbol
        self.strategy_cls = STRATEGIES[strategy]
        self.num_lots = num_lots
        self.strike_step = strike_step
        self.product = product
        # Extra per-strategy constructor kwargs beyond the shared ones above.
        self.strategy_kwargs = strategy_kwargs or {}
        self.broker = MockBroker(data_feed=self.data_feed, slippage_pct=slippage_pct)

        # Simulated compliance components
        self.audit = AuditTrail(audit_log_path="logs/mock_audit_trail.log")
        self.kill_switch = KillSwitch()
        self.rate_limiter = OrderRateLimiter(max_per_second=10)

    def run_simulation(self, entry_time: datetime | None = None) -> None:
        """
        Step through every real historical bar loaded into the data feed
        (`self.data_feed.timestamps`, populated by DataFeed.load_from_csv()).

        Triggers the strategy exactly once, at the first bar at/after
        `entry_time`. If `entry_time` is None, defaults to
        `data_feed.earliest_viable_time` — the first moment spot AND both
        option legs all already have a real print — rather than blindly the
        very first spot bar of the day, which commonly precedes a deep-OTM
        leg's first trade (entering before that leg has any price data is
        guaranteed to fail, not a meaningful backtest scenario). After
        entry, continues stepping through the remaining real bars so the
        final report can mark the position to the real historical closing
        price of the last bar in the dataset — this is a mark-to-market
        snapshot, NOT a true expiry settlement (that would require
        settlement-price data this engine doesn't fetch).

        Args:
            entry_time: Timestamp to enter the trade at. None = auto (see above).
        """
        timestamps = self.data_feed.timestamps
        if not timestamps:
            raise RuntimeError(
                "DataFeed has no data loaded. Call data_feed.load_from_csv() "
                "with real candles before running the simulation."
            )

        if entry_time is None and self.data_feed.earliest_viable_time is not None:
            entry_time = self.data_feed.earliest_viable_time
            log.info("No --entry-time given — defaulting to earliest viable entry: %s", entry_time)

        log.info(
            "Starting real-data backtest | %s | %d bars | %s → %s",
            self.symbol, len(timestamps), timestamps[0], timestamps[-1],
        )

        entered = False
        for ts in timestamps:
            self.data_feed.set_time(ts)
            if not entered and (entry_time is None or ts >= entry_time):
                self._execute_strategy()
                entered = True

        if not entered:
            log.warning(
                "entry_time=%s was after the last available bar (%s) — no trade was entered.",
                entry_time, timestamps[-1],
            )

        self._generate_report()

    def _execute_strategy(self) -> None:
        """Trigger the configured strategy at the current simulated time."""
        log.info("Triggering strategy '%s' at %s", self.strategy_cls.__name__, self.data_feed.current_time)
        try:
            strategy = self.strategy_cls(
                broker=self.broker,
                audit=self.audit,
                kill_switch=self.kill_switch,
                rate_limiter=self.rate_limiter,
                symbol=self.symbol,
                num_lots=self.num_lots,
                strike_step=self.strike_step,
                product=self.product,
                **self.strategy_kwargs,
            )
            strategy.run()
        except Exception as e:
            log.error("Strategy execution failed in backtest: %s", e, exc_info=True)

    def _generate_report(self) -> None:
        """
        Report real transaction-cost-adjusted P&L.

        Every fill in self.broker.orders already has real slippage baked
        into its execution_price (see MockBroker._place_order). On top of
        that, this pairs each SELL with any real closing BUY on the same
        instrument (FIFO — covers auto-unwind and any future exit logic) to
        compute REALIZED P&L using the two actual fill prices. Any SELL
        left without a matching BUY is still open, and is reported
        separately as UNREALIZED, mark-to-market against the last real bar
        in the dataset — it must not be summed into the same "closed trade"
        total as a realized fill, or costs/P&L get double-counted.

        All costs (brokerage, exchange transaction charges, GST, STT, SEBI
        charges, stamp duty) come from utils.costs.calculate_options_transaction_cost(),
        the same real Indian F&O tax/brokerage model used by run_paper_tracker.py.
        """
        log.info("=== Backtest Complete ===")
        log.info("Orders placed: %d", len(self.broker.orders))

        # Pair each SELL with its closing BUY (if any) on the same
        # instrument, FIFO, so auto-unwind buybacks are matched to the
        # SELL they closed instead of being treated as fresh entries.
        open_sells: dict[str, list] = {}
        closed_pairs: list = []
        for order in self.broker.orders:
            token = order["instrument_token"]
            if order["transaction_type"] == "SELL":
                open_sells.setdefault(token, []).append(order)
            elif order["transaction_type"] == "BUY":
                queue = open_sells.get(token)
                if queue:
                    closed_pairs.append((queue.pop(0), order))
                else:
                    log.warning(
                        "BUY order %s for %s has no matching open SELL — "
                        "excluded from P&L.", order["order_id"], token,
                    )

        total_entry_costs = 0.0
        total_exit_costs = 0.0
        total_realized_pnl = 0.0
        total_unrealized_pnl = 0.0

        for sell_order, buy_order in closed_pairs:
            entry_cost = calculate_options_transaction_cost(
                sell_order["execution_price"], sell_order["quantity"], "SELL"
            )
            exit_cost = calculate_options_transaction_cost(
                buy_order["execution_price"], buy_order["quantity"], "BUY"
            )
            gross_pnl = (sell_order["execution_price"] - buy_order["execution_price"]) * sell_order["quantity"]
            net_pnl = gross_pnl - entry_cost - exit_cost
            total_entry_costs += entry_cost
            total_exit_costs += exit_cost
            total_realized_pnl += net_pnl
            log.info(
                "  REALIZED | %s | SELL @ %.2f -> BUY @ %.2f | qty=%d | "
                "costs=₹%.2f | net_pnl=%+.2f",
                sell_order["instrument_token"], sell_order["execution_price"],
                buy_order["execution_price"], sell_order["quantity"],
                entry_cost + exit_cost, net_pnl,
            )

        # Any SELL still without a matching BUY is an open position — mark
        # it to market against the last real bar in the dataset instead.
        for token, queue in open_sells.items():
            for sell_order in queue:
                entry_cost = calculate_options_transaction_cost(
                    sell_order["execution_price"], sell_order["quantity"], "SELL"
                )
                total_entry_costs += entry_cost

                exit_price = self.data_feed.get_ltp(token)
                if exit_price is None:
                    log.info(
                        "  OPEN | %s | SELL @ %.2f | qty=%d | entry cost ₹%.2f | "
                        "no closing price available for mark-to-market",
                        token, sell_order["execution_price"], sell_order["quantity"], entry_cost,
                    )
                    continue

                exit_cost = calculate_options_transaction_cost(exit_price, sell_order["quantity"], "BUY")
                gross_pnl = (sell_order["execution_price"] - exit_price) * sell_order["quantity"]
                net_pnl = gross_pnl - entry_cost - exit_cost
                total_exit_costs += exit_cost
                total_unrealized_pnl += net_pnl
                log.info(
                    "  UNREALIZED (mark-to-market@%s) | %s | SELL @ %.2f | qty=%d | "
                    "close=%.2f | costs=₹%.2f | net_pnl=%+.2f",
                    self.data_feed.current_time, token, sell_order["execution_price"],
                    sell_order["quantity"], exit_price, entry_cost + exit_cost, net_pnl,
                )

        log.info("Total Entry (SELL) Transaction Costs: ₹%.2f", total_entry_costs)
        log.info("Total Exit (BUY) Transaction Costs: ₹%.2f", total_exit_costs)
        log.info("Total Realized Net P&L (actual closed trades, e.g. auto-unwind): ₹%+.2f", total_realized_pnl)
        log.info("Total Unrealized Net P&L (mark-to-market, still-open positions): ₹%+.2f", total_unrealized_pnl)
        log.info("Total Net P&L (realized + unrealized): ₹%+.2f", total_realized_pnl + total_unrealized_pnl)
        log.info("Positions held: %s", self.broker.positions)


if __name__ == "__main__":
    import argparse

    from automate.compliance.sebi_rules import init_market_calendar
    from automate.utils.logger import setup_logger

    # Configure the ROOT logger so every module's `get_logger(__name__)`
    # call actually produces output instead of being dropped.
    setup_logger(name="", level="INFO")

    # Load once, upfront — otherwise the market-hours compliance check
    # (called inside the strategy) lazy-loads it on first use with a
    # "was not called before use" warning.
    init_market_calendar(force=False)

    parser = argparse.ArgumentParser(
        description=(
            "Real historical-data backtest for the Short Strangle strategy. "
            "Simple usage: python3 -m automate.backtest.engine --symbol RELIANCE — "
            "reads data/historical/<symbol>_manifest.json, written automatically "
            "by scripts/download_real_history.py. Pass the per-contract flags "
            "below manually to override the manifest or backtest data that "
            "didn't come from that script (e.g. bhavcopy-derived CSVs)."
        )
    )
    parser.add_argument("--symbol", default="RELIANCE")
    parser.add_argument("--strategy", choices=list(STRATEGIES), default="ten_percent_otm_strangle",
                         help="Which strategy to backtest. Only one is implemented today.")
    parser.add_argument("--equity-key", default=None, help="e.g. NSE_EQ|INE002A01018")
    parser.add_argument("--spot-csv", default=None)
    parser.add_argument("--expiry", default=None, help="YYYY-MM-DD")
    parser.add_argument("--call-strike", type=int, default=None)
    parser.add_argument("--call-token", default=None, help="e.g. NSE_FO|12345")
    parser.add_argument("--call-csv", default=None)
    parser.add_argument("--put-strike", type=int, default=None)
    parser.add_argument("--put-token", default=None)
    parser.add_argument("--put-csv", default=None)
    parser.add_argument("--num-lots", type=int, default=1, help="How many real NSE lots per leg (quantity = num_lots x real lot size).")
    parser.add_argument(
        "--strike-step", type=float, default=None,
        help="Override the strike interval. Default: None = read from the manifest (the value "
             "actually used to decide which candles to download), or resolved dynamically from "
             "today's instrument master if there's no manifest — never a hardcoded guess.",
    )
    parser.add_argument(
        "--entry-time", default=None,
        help="ISO timestamp to enter at, e.g. 2026-07-01T09:20:00+05:30. Default: first bar.",
    )
    parser.add_argument(
        "--slippage-pct", type=float, default=0.005,
        help="Fractional slippage applied to every fill (0.005 = 0.5%%), "
             "worse-direction (lower on SELL, higher on BUY). Default matches "
             "a conservative real-world NSE F&O estimate for liquid monthly options.",
    )
    args = parser.parse_args()

    # Fill in any per-contract flag not given on the command line from
    # data/historical/<symbol>_manifest.json (written by
    # scripts/download_real_history.py) so `--symbol RELIANCE` alone works.
    manual_fields = [
        "equity_key", "spot_csv", "expiry", "call_strike", "call_token",
        "call_csv", "put_strike", "put_token", "put_csv",
    ]
    missing = [f for f in manual_fields if getattr(args, f) is None]
    if missing or args.strike_step is None:
        manifest_path = f"data/historical/{args.symbol}_manifest.json"
        if not os.path.exists(manifest_path):
            if missing:
                parser.error(
                    f"Missing {missing} and no manifest found at {manifest_path}. "
                    f"Run `python3 scripts/download_real_history.py --symbol {args.symbol}` "
                    f"first, or pass all the per-contract flags manually."
                )
            # No manifest but the per-contract flags were all given
            # manually (e.g. bhavcopy-derived data) — strike_step falls
            # back to dynamic resolution inside the strategy itself.
        else:
            with open(manifest_path) as f:
                manifest = json.load(f)
            # manifest keys from download_real_history.py's info dict use
            # *_path for CSVs; CLI flags use *_csv — map between them.
            field_map = {
                "equity_key": "equity_key", "spot_csv": "spot_path", "expiry": "expiry",
                "call_strike": "call_strike", "call_token": "call_token", "call_csv": "call_path",
                "put_strike": "put_strike", "put_token": "put_token", "put_csv": "put_path",
            }
            for arg_field, manifest_field in field_map.items():
                if getattr(args, arg_field) is None:
                    setattr(args, arg_field, manifest[manifest_field])
            if args.strike_step is None and "strike_step" in manifest:
                # Must match what download_real_history.py actually used to
                # decide which CE/PE candles to download — NOT re-resolved
                # independently here, since "today's real value" could have
                # drifted since the download if run on a different day.
                args.strike_step = manifest["strike_step"]
            log.info("Loaded contract details for %s from %s", args.symbol, manifest_path)

    feed = DataFeed()
    feed.load_from_csv(
        equity_key=args.equity_key,
        spot_csv_path=args.spot_csv,
        expiry=args.expiry,
        call_strike=args.call_strike,
        call_token=args.call_token,
        call_csv_path=args.call_csv,
        put_strike=args.put_strike,
        put_token=args.put_token,
        put_csv_path=args.put_csv,
    )

    engine = BacktestEngine(
        data_feed=feed,
        symbol=args.symbol,
        strategy=args.strategy,
        num_lots=args.num_lots,
        strike_step=args.strike_step,
        slippage_pct=args.slippage_pct,
    )
    entry_time = datetime.fromisoformat(args.entry_time) if args.entry_time else None
    engine.run_simulation(entry_time=entry_time)
