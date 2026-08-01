"""
backtest/custom_engine.py — generic backtest engine for user-built custom
strategies (RuleBasedStrategy), over the same real historical daily
bhavcopy data historical_engine.py uses for TenPercentOTMStrangle.

historical_engine.py's per-cycle P&L walk is hardcoded to exactly 2 legs
(a CE + a PE, both SELL) — this file is the same cycle-discovery /
day-by-day SL-TP-expiry walk, generalized to N legs of mixed BUY/SELL,
driven by RuleBasedStrategy instead of a single hand-written strategy
class. BhavcopyDataFeed itself already supports any strike/option_type
combination unmodified.

CAVEATS (same ones historical_engine.py documents — repeated here since
this is the entry point users actually hit from the strategy builder's
"Backtest" button):
  - Real DAILY EOD bhavcopy close prices, not intraday ticks — entry/exit
    fills are simulated off the day's close plus MockBroker's slippage_pct
    (same model PaperBroker uses live), not a real intraday fill.
  - A leg's daily close can be a stale/zero-volume carry-forward with no
    real trade behind it, most likely for deep OTM strikes — see
    `liquid` in each cycle result.
  - Equity/future legs use the daily future close as a spot proxy (no
    cash-equity close in this dataset).
"""
from datetime import date, datetime, time as dtime
from typing import Callable, Dict, List, Optional

from sqlalchemy import text
from automate.db.engine import SessionLocal

from automate.backtest.bhavcopy_data_feed import BhavcopyDataFeed
from automate.broker.mock_broker import MockBroker
from automate.compliance.sebi_rules import AuditTrail, KillSwitch, OrderRateLimiter
from automate.strategies.custom.rule_strategy import RuleBasedStrategy
from automate.utils.costs import calculate_options_transaction_cost_breakdown, sum_breakdowns
from automate.utils.instrument_cache import InstrumentCache
from automate.utils.logger import get_logger
from automate.utils.option_utils import check_exit_trigger, is_within_pre_expiry_buffer
from automate.utils import black76

log = get_logger(__name__)

_MARKET_OPEN = dtime(9, 20)


def compute_nifty_benchmark_return(from_date: str, to_date: str) -> Optional[float]:
    """
    NIFTY 50 buy-and-hold %% return over [from_date, to_date] — the
    standard Indian-market benchmark a strategy backtest is compared
    against (see utils/backtest_stats.py's alpha_pct). Uses the same
    near-month-future-close proxy the rest of this engine already uses for
    spot (fno_bhavcopy has no cash-index close, see module docstring) —
    not a separate data source. A plain module function (not an
    CustomRuleBacktestEngine method) since it's about NIFTY specifically,
    independent of whatever symbol the caller is actually backtesting.

    Returns None if either endpoint has no NIFTY FUTIDX print nearby
    (e.g. the requested range is outside the downloaded bhavcopy history).
    """
    session = SessionLocal()
    try:
        start_row = session.execute(
            text(
                "SELECT close FROM fno_bhavcopy WHERE symbol='NIFTY' AND instrument='FUTIDX' "
                "AND trade_date >= :d AND expiry_dt >= trade_date ORDER BY trade_date ASC LIMIT 1"
            ),
            {"d": from_date},
        ).fetchone()
        end_row = session.execute(
            text(
                "SELECT close FROM fno_bhavcopy WHERE symbol='NIFTY' AND instrument='FUTIDX' "
                "AND trade_date <= :d AND expiry_dt >= trade_date ORDER BY trade_date DESC LIMIT 1"
            ),
            {"d": to_date},
        ).fetchone()
        if not start_row or not end_row or not start_row[0] or not end_row[0]:
            return None
        start_price = float(start_row[0])
        if start_price <= 0:
            return None
        return (float(end_row[0]) / start_price - 1.0) * 100.0
    finally:
        session.close()


class CustomRuleBacktestEngine:
    """Simulates a RuleBasedStrategy across every historical monthly expiry cycle in a date range."""

    def __init__(
        self,
        symbol: str,
        rules: dict,
        strike_step: Optional[float] = None,
        product: str = "NRML",
        option_instrument: str = "OPTSTK",
        future_instrument: str = "FUTSTK",
        audit_log_path: str = "logs/mock_audit_trail.log",
    ) -> None:
        self.symbol = symbol.upper()
        self.rules = rules
        self.strike_step = strike_step
        self.product = product
        self.option_instrument = option_instrument
        self.future_instrument = future_instrument

        self.session = SessionLocal()
        equity_key = InstrumentCache().resolve_key(self.symbol)
        if not equity_key:
            raise RuntimeError(
                f"Could not resolve an instrument key for '{self.symbol}' from the cached Upstox "
                f"instrument master. Run `python3 scripts/download_real_history.py --symbol {self.symbol}` first."
            )
        self.equity_key = equity_key

        self.feed = BhavcopyDataFeed(self.session, self.symbol, equity_key, option_instrument, future_instrument)
        # Same 0.1% default PaperBroker uses for live paper trading (see
        # broker/mock_broker.py's default) — a backtest with ZERO slippage
        # (the old value here) is systematically more optimistic than what
        # paper/live trading actually experiences on every fill, making
        # backtest-vs-paper-vs-live numbers not truly comparable.
        self.broker = MockBroker(data_feed=self.feed)
        self.audit = AuditTrail(audit_log_path=audit_log_path)
        self.rate_limiter = OrderRateLimiter(max_per_second=10)

    def discover_cycles(self, from_date: Optional[str], to_date: Optional[str]) -> List[dict]:
        expiries = [
            r[0] for r in self.session.execute(
                text(
                    "SELECT DISTINCT expiry_dt FROM fno_bhavcopy WHERE symbol=:symbol AND instrument=:instrument "
                    "AND expiry_dt IS NOT NULL ORDER BY expiry_dt"
                ),
                {"symbol": self.symbol, "instrument": self.option_instrument},
            ).fetchall()
        ]
        if (self.rules.get("expiry") or {}).get("mode") == "MONTHLY":
            # Keep only the LAST listed expiry in each calendar month —
            # same "monthly" convention as
            # utils.option_utils.find_nearest_expiry_by_type(), which the
            # live/paper engine uses — so backtest and live agree on what
            # "monthly" means for the same rules.
            by_month: dict = {}
            for exp in expiries:
                d = date.fromisoformat(exp)
                key = (d.year, d.month)
                if key not in by_month or exp > by_month[key]:
                    by_month[key] = exp
            expiries = sorted(by_month.values())
        fut_dates = [
            r[0] for r in self.session.execute(
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

    def run(
        self,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> List[dict]:
        """
        `on_progress(done, total)`, if given, is called after each cycle —
        used by the async /backtest route to update BacktestRun.progress_
        current so the frontend's polling can show a real progress bar
        instead of a bare spinner on a long (many-decades) history.
        """
        results = []
        try:
            cycles = self.discover_cycles(from_date, to_date)
            total = len(cycles)
            for i, cycle in enumerate(cycles):
                row = self._run_one_cycle(cycle)
                if row is not None:
                    results.append(row)
                if on_progress:
                    on_progress(i + 1, total)
        finally:
            self.session.close()
        return results

    def _combined_pnl_pct(self, legs: List[dict], now_prices: Dict[str, Optional[float]]) -> Optional[float]:
        """
        Generalization of strangle_pnl_pct() to N legs of mixed BUY/SELL:
        signed mark-to-market P&L (in currency) divided by total premium
        involved at entry (positive = profit, matching check_exit_trigger's
        convention). Returns None if any leg's current price is missing
        that day (data gap — caller should keep holding).
        """
        pnl_amount = 0.0
        denom = 0.0
        for leg in legs:
            now = now_prices.get(leg["instrument_token"])
            if now is None:
                return None
            sign = -1 if leg["transaction_type"] == "SELL" else 1
            pnl_amount += (leg["entry_price"] - now) * leg["quantity"] * (-sign)
            denom += leg["entry_price"] * leg["quantity"]
        if denom <= 0:
            return 0.0
        return pnl_amount / denom * 100.0

    def _attach_entry_greeks(self, legs: List[dict], futures_price_at_entry: float, entry_date: str, expiry: str) -> None:
        """
        Mutates each leg dict in place, adding a "greeks_at_entry" key —
        IV/Delta/Gamma/Theta/Vega/Rho solved from the leg's real entry
        fill price via Black-76 (see utils/black76.py). `futures_price_at_entry`
        is BhavcopyDataFeed's equity_key price, which for this dataset IS
        the futures close (no cash-equity close available) — already the
        correct F input for Black-76, no separate lookup needed here (this
        is backtest-only; the live/paper path resolves a real futures
        instrument_key instead, see api/routes_custom_strategies.py's
        greeks endpoint).

        None (not a raised error) on a leg whose IV can't be solved (e.g.
        a stale/zero-volume entry print) — Greeks are supplementary
        analysis on top of the real P&L walk, never something that should
        abort a backtest cycle.
        """
        days_to_expiry = (date.fromisoformat(expiry) - date.fromisoformat(entry_date)).days
        T = max(days_to_expiry, 1) / 365.0
        for leg in legs:
            leg["greeks_at_entry"] = black76.compute_greeks_from_market_price(
                F=futures_price_at_entry, K=leg["strike"], T=T,
                r=black76.DEFAULT_RISK_FREE_RATE, market_price=leg["entry_price"],
                option_type=leg["option_type"],
            )

    def _run_one_cycle(self, cycle: dict) -> Optional[dict]:
        entry_date, expiry, exit_date = cycle["entry_date"], cycle["expiry"], cycle["exit_date"]
        self.feed.set_time(datetime.combine(date.fromisoformat(entry_date), _MARKET_OPEN))

        strategy = RuleBasedStrategy(
            broker=self.broker,
            audit=self.audit,
            kill_switch=KillSwitch(),
            rate_limiter=self.rate_limiter,
            symbol=self.symbol,
            rules=self.rules,
            strike_step=self.strike_step,
            product=self.product,
        )
        result = strategy.run()
        if result.get("status") != "success":
            log.info("Cycle entry=%s expiry=%s skipped: %s", entry_date, expiry, result.get("error", result.get("status")))
            return None

        legs = result["legs"]
        # RuleBasedStrategy.execute() places each entry order through the
        # broker (which DOES apply MockBroker.slippage_pct) but then
        # overwrites leg["entry_price"] with a fresh, un-slipped
        # broker.get_ltp() call (see rule_strategy.py) — a real gap in the
        # shared live/paper/backtest strategy code, out of scope to fix
        # broadly here (that's a live-trading-accounting change with real
        # money impact, not a backtest-only one). Corrected HERE, backtest-
        # only, by reading the actual fill back from the broker's own order
        # record — without it, the exit-slippage fix below would make exits
        # look artificially worse than entries instead of genuinely
        # symmetric.
        orders_by_id = {o["order_id"]: o for o in self.broker.orders}
        for leg in legs:
            order = orders_by_id.get(leg.get("order_id"))
            if order is not None:
                leg["entry_price"] = order["execution_price"]
        self._attach_entry_greeks(legs, result["spot_price"], entry_date, expiry)

        exit_reason = "EXPIRY"
        exit_ = self.rules.get("exit") or {}
        take_profit_pct = exit_.get("take_profit_pct")
        stop_loss_pct = exit_.get("stop_loss_pct")
        exit_days_before_expiry = exit_.get("exit_days_before_expiry", 0)

        option_tokens = [leg["instrument_token"] for leg in legs]
        if option_tokens and (take_profit_pct is not None or stop_loss_pct is not None or exit_days_before_expiry):
            expiry_date = date.fromisoformat(expiry)
            trading_days = [
                r[0] for r in self.session.execute(
                    text(
                        "SELECT DISTINCT trade_date FROM fno_bhavcopy WHERE symbol=:symbol AND instrument=:instrument AND "
                        "expiry_dt=:expiry AND trade_date > :entry_date AND trade_date <= :exit_date ORDER BY trade_date"
                    ),
                    {"symbol": self.symbol, "instrument": self.option_instrument, "expiry": expiry,
                     "entry_date": entry_date, "exit_date": exit_date},
                ).fetchall()
            ]
            for day in trading_days:
                self.feed.set_time(datetime.combine(date.fromisoformat(day), _MARKET_OPEN))
                now_prices = {tok: self.feed.get_ltp(tok) for tok in option_tokens}
                pnl_pct = self._combined_pnl_pct(legs, now_prices)
                if pnl_pct is None:
                    continue
                trigger = check_exit_trigger(pnl_pct, take_profit_pct, stop_loss_pct)
                if trigger is None and exit_days_before_expiry and is_within_pre_expiry_buffer(
                    date.fromisoformat(day), expiry_date, exit_days_before_expiry
                ):
                    trigger = "EXPIRY"
                if trigger:
                    exit_date = day
                    exit_reason = trigger
                    break

        self.feed.set_time(datetime.combine(date.fromisoformat(exit_date), _MARKET_OPEN))
        gross_pnl = 0.0
        net_pnl = 0.0
        leg_charges = []
        liquid = True
        for idx, leg in enumerate(legs):
            token = leg["instrument_token"]
            exit_transaction_type = "BUY" if leg["transaction_type"] == "SELL" else "SELL"
            if leg["instrument_type"] == "OPTION":
                raw_ltp = self.feed.get_ltp(token)
                if raw_ltp is None:
                    log.warning("Cycle entry=%s expiry=%s: no exit price for %s — skipping cycle.", entry_date, expiry, token)
                    return None
                if self.feed.get_volume(token) == 0:
                    liquid = False
                # Route the exit through the broker (same as entry) rather
                # than reading feed LTP directly — entries already pick up
                # MockBroker's slippage_pct via RuleBasedStrategy's order
                # placement; reading exit price straight off the feed made
                # every backtest systematically more optimistic leaving a
                # position than entering one. See custom_strategy_scheduler
                # ._try_exit() for the same pattern in live/paper trading.
                exit_fn = self.broker.place_buy_order if exit_transaction_type == "BUY" else self.broker.place_sell_order
                exit_order_id = exit_fn(instrument_token=token, quantity=leg["quantity"], product=self.product, tag=f"BT_EXIT_{idx}")
                exit_price = self.broker.orders[-1]["execution_price"] if exit_order_id else raw_ltp
                leg["exit_order_id"] = exit_order_id
                leg["exit_price"] = round(exit_price, 4)
                sign = 1 if leg["transaction_type"] == "SELL" else -1
                leg_gross = (leg["entry_price"] - exit_price) * leg["quantity"] * sign
                entry_costs = calculate_options_transaction_cost_breakdown(leg["entry_price"], leg["quantity"], leg["transaction_type"])
                exit_costs = calculate_options_transaction_cost_breakdown(exit_price, leg["quantity"], exit_transaction_type)
                leg_charges.extend([entry_costs, exit_costs])
                total_charges = entry_costs.get("total", 0) + exit_costs.get("total", 0)
                gross_pnl += leg_gross
                net_pnl += leg_gross - total_charges
            else:
                raw_ltp = self.feed.get_ltp(self.equity_key)
                exit_fn = self.broker.place_buy_order if exit_transaction_type == "BUY" else self.broker.place_sell_order
                exit_order_id = exit_fn(instrument_token=self.equity_key, quantity=leg["quantity"], product=self.product, tag=f"BT_EXIT_{idx}") if raw_ltp is not None else None
                exit_price = self.broker.orders[-1]["execution_price"] if exit_order_id else (raw_ltp or leg["entry_price"])
                leg["exit_order_id"] = exit_order_id
                leg["exit_price"] = round(exit_price, 4)
                sign = 1 if leg["transaction_type"] == "BUY" else -1
                leg_gross = (exit_price - leg["entry_price"]) * leg["quantity"] * sign
                gross_pnl += leg_gross
                net_pnl += leg_gross

        entry_premium_total = sum(leg["entry_price"] * leg["quantity"] for leg in legs)
        pnl_pct_of_premium = (net_pnl / entry_premium_total * 100.0) if entry_premium_total > 0 else 0.0
        charges_total = sum_breakdowns(*leg_charges).get("total", 0) if leg_charges else 0.0

        return {
            "entry_date": entry_date,
            "expiry": expiry,
            "exit_date": exit_date,
            "exit_reason": exit_reason,
            "spot_at_entry": result["spot_price"],
            "legs": [
                {"instrument_type": l["instrument_type"], "option_type": l["option_type"], "strike": l["strike"],
                 "transaction_type": l["transaction_type"], "quantity": l["quantity"], "entry_price": l["entry_price"],
                 "exit_price": l.get("exit_price"), "exit_order_id": l.get("exit_order_id"),
                 "greeks_at_entry": l.get("greeks_at_entry")}
                for l in legs
            ],
            "gross_pnl": round(gross_pnl, 2),
            "charges": round(charges_total, 2),
            "net_pnl": round(net_pnl, 2),
            "pnl_pct_of_premium": round(pnl_pct_of_premium, 2),
            "won": net_pnl > 0,
            "liquid": liquid,
        }
