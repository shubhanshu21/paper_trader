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

An all-EQUITY strategy (every leg instrument_type == "EQUITY") does NOT
go through discover_cycles()/_run_one_cycle() at all — see run_equity()
below. Equity has no expiry to discover a "cycle" from, so that whole
per-expiry-cycle shape doesn't apply; instead it's a continuous walk-
forward over every real trading day in range, re-entering as soon as no
position is open and the entry condition is met, holding until
take_profit_pct/stop_loss_pct fires. A strategy that mixes EQUITY and
OPTION legs together still uses the OPTION-cycle path (that's the more
restrictive, safer default — equity's own semantics only kick in for a
strategy with NOTHING but equity legs).
"""
from collections.abc import Callable
from datetime import date, datetime
from datetime import time as dtime

from sqlalchemy import text

from backtest.bhavcopy_data_feed import BhavcopyDataFeed
from broker.mock_broker import MockBroker
from compliance.sebi_rules import AuditTrail, KillSwitch, OrderRateLimiter
from db.engine import SessionLocal
from strategies.custom.rule_strategy import RuleBasedStrategy
from utils import black76
from utils.costs import (
    calculate_equity_transaction_cost_breakdown,
    calculate_options_transaction_cost_breakdown,
    sum_breakdowns,
)
from utils.instrument_cache import InstrumentCache
from utils.logger import get_logger
from utils.option_utils import check_exit_trigger, is_within_pre_expiry_buffer
from utils.trailing_stop import advance_trailing_stop, stop_triggered

log = get_logger(__name__)

_MARKET_OPEN = dtime(9, 20)


def compute_nifty_benchmark_return(from_date: str, to_date: str) -> float | None:
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
        strike_step: float | None = None,
        product: str = "NRML",
        option_instrument: str = "OPTSTK",
        future_instrument: str = "FUTSTK",
        audit_log_path: str = "logs/mock_audit_trail.log",
        charge_rates: dict | None = None,
    ) -> None:
        self.symbol = symbol.upper()
        self.rules = rules
        self.strike_step = strike_step
        self.product = product
        self.option_instrument = option_instrument
        self.future_instrument = future_instrument
        # This strategy owner's F&O charge-rate overrides (see
        # utils/wallet.py's get_charge_rates()) — defaults to
        # utils/costs.py's DEFAULT_RATES when not passed (e.g. CLI/dev tools
        # with no per-user concept).
        self.charge_rates = charge_rates

        self.session = SessionLocal()
        equity_key = InstrumentCache().resolve_key(self.symbol)
        if not equity_key:
            raise RuntimeError(
                f"Could not resolve an instrument key for '{self.symbol}' from the cached Upstox "
                f"instrument master. Run `python3 scripts/download_real_history.py --symbol {self.symbol}` first."
            )
        self.equity_key = equity_key

        self.feed = BhavcopyDataFeed(self.session, self.symbol, equity_key, option_instrument, future_instrument)
        # Read slippage from rules (default to 0.1%)
        self.slippage_pct = self.rules.get("slippage_pct", 0.1) / 100.0
        self.broker = MockBroker(data_feed=self.feed, slippage_pct=self.slippage_pct)
        self.audit = AuditTrail(audit_log_path=audit_log_path)
        self.rate_limiter = OrderRateLimiter(max_per_second=10)

    def _driving_expiry_mode(self) -> str:
        """
        For a calendar spread (legs on different expiry_mode streams — see
        rule_schema.py's leg.expiry_mode), cycles are discovered off
        whichever mode expires MORE OFTEN — WEEKLY if any leg needs it,
        else MONTHLY. Each leg still resolves its OWN expiry independently
        at every cycle's entry_date (RuleBasedStrategy._resolve_expiries_and_chains),
        this only decides how often a new cycle's entry_date is tried. A
        single-expiry strategy has exactly one mode here — identical to
        today's behavior.
        """
        modes = {
            leg.get("expiry_mode") or (self.rules.get("expiry") or {}).get("mode", "WEEKLY")
            for leg in self.rules["legs"]
        }
        return "WEEKLY" if "WEEKLY" in modes else "MONTHLY"

    def _natural_exit_date(self, expiry: str, cache: dict[str, str | None]) -> str | None:
        """The last real trading day for a given expiry_dt — a leg's own contract lifetime end, memoized per cycle."""
        if expiry in cache:
            return cache[expiry]
        row = self.session.execute(
            text(
                "SELECT MAX(trade_date) FROM fno_bhavcopy WHERE symbol=:symbol AND instrument=:instrument "
                "AND expiry_dt=:expiry"
            ),
            {"symbol": self.symbol, "instrument": self.option_instrument, "expiry": expiry},
        ).fetchone()
        result = row[0] if row else None
        cache[expiry] = result
        return result

    def _trading_days(self, expiry: str, entry_date: str, exit_date: str) -> list[str]:
        return [
            r[0] for r in self.session.execute(
                text(
                    "SELECT DISTINCT trade_date FROM fno_bhavcopy WHERE symbol=:symbol AND instrument=:instrument AND "
                    "expiry_dt=:expiry AND trade_date > :entry_date AND trade_date <= :exit_date ORDER BY trade_date"
                ),
                {"symbol": self.symbol, "instrument": self.option_instrument, "expiry": expiry,
                 "entry_date": entry_date, "exit_date": exit_date},
            ).fetchall()
        ]

    def _leg_pnl_pct(self, leg: dict, now_price: float | None) -> float | None:
        """Single-leg version of _combined_pnl_pct, for an individually-managed leg's own TP/SL (see rule_schema.py's leg.exit)."""
        if now_price is None:
            return None
        sign = -1 if leg["transaction_type"] == "SELL" else 1
        pnl_amount = (leg["entry_price"] - now_price) * leg["quantity"] * (-sign)
        denom = leg["entry_price"] * leg["quantity"]
        if denom <= 0:
            return 0.0
        return pnl_amount / denom * 100.0

    def discover_cycles(self, from_date: str | None, to_date: str | None) -> list[dict]:
        driving_mode = self._driving_expiry_mode()
        expiries = [
            r[0] for r in self.session.execute(
                text(
                    "SELECT DISTINCT expiry_dt FROM fno_bhavcopy WHERE symbol=:symbol AND instrument=:instrument "
                    "AND expiry_dt IS NOT NULL ORDER BY expiry_dt"
                ),
                {"symbol": self.symbol, "instrument": self.option_instrument},
            ).fetchall()
        ]
        if driving_mode == "MONTHLY":
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

    def _is_all_equity(self) -> bool:
        """True iff every leg is instrument_type EQUITY — an OPTION-cycle strategy (or a mixed EQUITY+OPTION one) always uses discover_cycles()/_run_one_cycle() instead, see run()."""
        return all((leg.get("instrument_type") or "OPTION") == "EQUITY" for leg in self.rules["legs"])

    def run(
        self,
        from_date: str | None = None,
        to_date: str | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[dict]:
        """
        `on_progress(done, total)`, if given, is called after each cycle —
        used by the async /backtest route to update BacktestRun.progress_
        current so the frontend's polling can show a real progress bar
        instead of a bare spinner on a long (many-decades) history.
        """
        results = []
        try:
            if self._is_all_equity():
                return self.run_equity(from_date, to_date, on_progress)
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

    def _equity_trading_days(self, from_date: str | None, to_date: str | None) -> list[str]:
        """Every real trading day in range, off the SAME future_instrument table discover_cycles() uses for its own calendar — no separate equity-cash calendar exists in this dataset (see module docstring)."""
        query = "SELECT DISTINCT trade_date FROM fno_bhavcopy WHERE symbol=:symbol AND instrument=:instrument"
        params: dict = {"symbol": self.symbol, "instrument": self.future_instrument}
        if from_date:
            query += " AND trade_date >= :from_date"
            params["from_date"] = from_date
        if to_date:
            query += " AND trade_date <= :to_date"
            params["to_date"] = to_date
        query += " ORDER BY trade_date ASC"
        return [r[0] for r in self.session.execute(text(query), params).fetchall()]

    def _equity_ma_condition_met(self, closes_before_today: list[float], period: int, direction: str, today_price: float) -> bool:
        """Mirrors api/custom_strategy_scheduler.py's _ma_crossover_met() EXACTLY: today's price vs the trailing N-day average of the closes BEFORE today (today's own price is never in its own average) — same live/backtest parity every other entry condition in this app keeps."""
        if not isinstance(period, int) or period < 2 or len(closes_before_today) < period:
            return False
        moving_average = sum(closes_before_today[-period:]) / period
        return today_price > moving_average if direction == "ABOVE" else today_price < moving_average

    def _equity_rsi_condition_met(self, closes_before_today: list[float], period: int, operator: str, threshold: float, today_price: float) -> bool:
        """Live/backtest parity for api/custom_strategy_scheduler.py's _rsi_condition_met() — RSI computed over the trailing closes PLUS today's own price as the final data point (RSI needs a full series of deltas up to "now," unlike the MA condition above which excludes today by design)."""
        if not isinstance(period, int) or period < 2 or len(closes_before_today) < period:
            return False
        from utils.technical_indicators import rsi
        series = [*closes_before_today[-period:], today_price]
        latest = rsi(series, period)[-1]
        if latest is None:
            return False
        return latest > threshold if operator == "ABOVE" else latest < threshold

    def _equity_bollinger_width_condition_met(self, closes_before_today: list[float], period: int, operator: str, threshold: float, today_price: float) -> bool:
        """Live/backtest parity for api/custom_strategy_scheduler.py's _bollinger_width_condition_met()."""
        if not isinstance(period, int) or period < 2 or len(closes_before_today) < period - 1:
            return False
        from utils.technical_indicators import bollinger_bands
        series = [*closes_before_today[-(period - 1):], today_price] if period > 1 else [today_price]
        if len(series) < period:
            return False
        latest = bollinger_bands(series, period)[-1]
        if latest is None or latest["width"] is None:
            return False
        return latest["width"] > threshold if operator == "ABOVE" else latest["width"] < threshold

    def run_equity(
        self,
        from_date: str | None,
        to_date: str | None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[dict]:
        """
        Walk-forward backtest for an all-EQUITY strategy — genuinely
        different shape from _run_one_cycle()'s per-expiry-cycle walk
        (equity has no expiry to discover cycles from at all). Re-enters
        as soon as no position is open and the entry condition is met
        (IMMEDIATE/AT_TIME = any day; CONDITIONAL/MA_CROSSOVER/RSI/
        BOLLINGER_WIDTH computed for real off this symbol's own historical
        closes), holds until
        take_profit_pct/stop_loss_pct fires. AT_TIME's clock-time and
        BEFORE_EXPIRY are meaningless at this daily-EOD-bhavcopy
        granularity (equity has no expiry) — both are treated as
        IMMEDIATE-equivalent (any day eligible) here; rule_schema.py's
        own validation already rejects BEFORE_EXPIRY for an all-equity
        strategy, so that case can't reach this method in practice.
        """
        trading_days = self._equity_trading_days(from_date, to_date)
        if len(trading_days) < 2:
            return []

        entry_rule = self.rules.get("entry") or {}
        condition = entry_rule.get("condition") or {}
        exit_ = self.rules.get("exit") or {}
        take_profit_pct = exit_.get("take_profit_pct")
        stop_loss_pct = exit_.get("stop_loss_pct")

        results: list[dict] = []
        open_legs: list[dict] | None = None
        open_entry_date: str | None = None
        closes_so_far: list[float] = []
        total = len(trading_days)

        for i, day in enumerate(trading_days):
            self.feed.set_time(datetime.combine(date.fromisoformat(day), _MARKET_OPEN))
            price = self.feed.get_ltp(self.equity_key)
            if price is None:
                continue

            if open_legs is None:
                eligible = entry_rule.get("mode") != "CONDITIONAL"
                if entry_rule.get("mode") == "CONDITIONAL" and condition.get("type") == "MA_CROSSOVER":
                    eligible = self._equity_ma_condition_met(closes_so_far, condition.get("period_days"), condition.get("direction"), price)
                elif entry_rule.get("mode") == "CONDITIONAL" and condition.get("type") == "RSI":
                    eligible = self._equity_rsi_condition_met(closes_so_far, condition.get("period_days"), condition.get("operator"), condition.get("threshold"), price)
                elif entry_rule.get("mode") == "CONDITIONAL" and condition.get("type") == "BOLLINGER_WIDTH":
                    eligible = self._equity_bollinger_width_condition_met(closes_so_far, condition.get("period_days"), condition.get("operator"), condition.get("threshold"), price)
                if eligible:
                    try:
                        strategy = RuleBasedStrategy(
                            broker=self.broker, audit=self.audit, kill_switch=KillSwitch(), rate_limiter=self.rate_limiter,
                            symbol=self.symbol, rules=self.rules, strike_step=self.strike_step, product=self.product,
                            notify_on_failure=False,
                        )
                        result = strategy.run()
                    except Exception as exc:
                        log.warning("Equity entry=%s failed: %s", day, exc)
                        result = {"status": "failed", "error": str(exc)}
                    if result.get("status") == "success":
                        legs = result["legs"]
                        orders_by_id = {o["order_id"]: o for o in self.broker.orders}
                        for leg in legs:
                            order = orders_by_id.get(leg.get("order_id"))
                            if order is not None:
                                leg["entry_price"] = order["execution_price"]
                        open_legs, open_entry_date = legs, day
            else:
                now_prices = {leg["instrument_token"]: price for leg in open_legs}
                pnl_pct = self._combined_pnl_pct(open_legs, now_prices)
                trigger = check_exit_trigger(pnl_pct, take_profit_pct, stop_loss_pct) if pnl_pct is not None else None
                if trigger:
                    leg_charges = []
                    gross_pnl = 0.0
                    for leg in open_legs:
                        opposite = self.broker.place_buy_order if leg["transaction_type"] == "SELL" else self.broker.place_sell_order
                        exit_side = "BUY" if leg["transaction_type"] == "SELL" else "SELL"
                        order_id = opposite(instrument_token=leg["instrument_token"], quantity=leg["quantity"], product=self.product, order_type="MARKET", tag="BT_EQUITY_EXIT")
                        fill_price = self.broker.orders[-1]["execution_price"] if order_id and self.broker.orders else price
                        leg["exit_price"], leg["exit_date"], leg["exit_reason"] = fill_price, day, trigger
                        sign = -1 if leg["transaction_type"] == "SELL" else 1
                        gross_pnl += (leg["entry_price"] - fill_price) * leg["quantity"] * (-sign)
                        leg_charges.append(calculate_equity_transaction_cost_breakdown(leg["entry_price"], leg["quantity"], leg["transaction_type"], self.charge_rates))
                        leg_charges.append(calculate_equity_transaction_cost_breakdown(fill_price, leg["quantity"], exit_side, self.charge_rates))

                    charges_total = sum_breakdowns(*leg_charges).get("total", 0)
                    net_pnl = gross_pnl - charges_total
                    entry_premium_total = sum(leg["entry_price"] * leg["quantity"] for leg in open_legs)
                    pnl_pct_of_premium = (net_pnl / entry_premium_total * 100.0) if entry_premium_total > 0 else 0.0

                    results.append({
                        "entry_date": open_entry_date,
                        "expiry": None,
                        "exit_date": day,
                        "exit_reason": trigger,
                        "spot_at_entry": None,
                        "legs": [
                            {"instrument_type": "EQUITY", "option_type": None, "strike": None,
                             "transaction_type": leg["transaction_type"], "quantity": leg["quantity"], "entry_price": leg["entry_price"],
                             "expiry": None, "exit_date": leg["exit_date"], "exit_reason": leg["exit_reason"],
                             "exit_price": leg["exit_price"], "exit_order_id": None, "greeks_at_entry": None}
                            for leg in open_legs
                        ],
                        "gross_pnl": round(gross_pnl, 2),
                        "charges": round(charges_total, 2),
                        "net_pnl": round(net_pnl, 2),
                        "pnl_pct_of_premium": round(pnl_pct_of_premium, 2),
                        "won": net_pnl > 0,
                        # No per-day traded-volume figure exists for cash equity in this
                        # dataset (only the derivative contract's own `contracts` column
                        # does — see module docstring) — always True rather than a guess.
                        "liquid": True,
                    })
                    open_legs, open_entry_date = None, None

            closes_so_far.append(price)
            if on_progress:
                on_progress(i + 1, total)

        return results

    def _combined_pnl_pct(self, legs: list[dict], now_prices: dict[str, float | None]) -> float | None:
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

    def _attach_entry_greeks(self, legs: list[dict], futures_price_at_entry: float, entry_date: str) -> None:
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

        Uses each leg's OWN resolved `expiry` (not a shared cycle expiry)
        for days-to-expiry — required for a calendar spread, where legs
        can be on different expiry cycles; harmless no-op difference for
        a single-expiry strategy since every leg's `expiry` is then
        identical anyway.

        None (not a raised error) on a leg whose IV can't be solved (e.g.
        a stale/zero-volume entry print) — Greeks are supplementary
        analysis on top of the real P&L walk, never something that should
        abort a backtest cycle.
        """
        entry_dt = date.fromisoformat(entry_date)
        for leg in legs:
            days_to_expiry = (date.fromisoformat(leg["expiry"]) - entry_dt).days
            T = max(days_to_expiry, 1) / 365.0
            leg["greeks_at_entry"] = black76.compute_greeks_from_market_price(
                F=futures_price_at_entry, K=leg["strike"], T=T,
                r=black76.DEFAULT_RISK_FREE_RATE, market_price=leg["entry_price"],
                option_type=leg["option_type"],
            )

    def _run_one_cycle(self, cycle: dict) -> dict | None:
        entry_date = cycle["entry_date"]
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
            notify_on_failure=False,
        )
        result = strategy.run()
        if result.get("status") != "success":
            log.info("Cycle entry=%s skipped: %s", entry_date, result.get("error", result.get("status")))
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
        self._attach_entry_greeks(legs, result["spot_price"], entry_date)

        # Each leg's own contract lifetime end (its own `expiry`, resolved
        # independently per rule_strategy.py — identical across all legs
        # for a single-expiry strategy, genuinely different for a calendar
        # spread). Nothing can be held past this regardless of any TP/SL.
        # When every leg shares the same expiry (a single-expiry strategy —
        # today's only case), reuse cycle["exit_date"] (computed identically
        # by discover_cycles' own query) for all of them rather than
        # re-querying. Only a genuine calendar spread (legs whose resolved
        # `expiry` actually differ from each other) needs its own per-leg
        # DB lookup here.
        natural_exit_cache: dict[str, str | None] = {}
        cycle_exit_date = cycle.get("exit_date")
        leg_expiries = {leg.get("expiry") for leg in legs if leg.get("expiry")}
        uniform_expiry = len(leg_expiries) <= 1
        for leg in legs:
            leg_expiry = leg.get("expiry")
            if uniform_expiry or not leg_expiry:
                natural = cycle_exit_date
            else:
                natural = self._natural_exit_date(leg_expiry, natural_exit_cache)
            if not natural or natural <= entry_date:
                log.warning(
                    "Cycle entry=%s: no exit data for leg %s expiry=%s — skipping cycle.",
                    entry_date, leg["instrument_token"], leg.get("expiry"),
                )
                return None
            leg["_natural_exit_date"] = natural

        # Split legs into individually-managed (own exit/trailing config —
        # rule_schema.py's leg.exit) vs combined-managed (participate in
        # the strategy-level TP/SL/exit_days_before_expiry check, today's
        # only behavior). idx == index into self.rules["legs"] since this
        # method always runs the FULL strategy (no leg_indices subset).
        exit_ = self.rules.get("exit") or {}
        strategy_take_profit_pct = exit_.get("take_profit_pct")
        strategy_stop_loss_pct = exit_.get("stop_loss_pct")
        strategy_exit_days_before_expiry = exit_.get("exit_days_before_expiry", 0)

        individually_managed = []
        combined_managed = []
        for idx, _leg in enumerate(legs):
            rules_leg = self.rules["legs"][idx] if idx < len(self.rules["legs"]) else {}
            if rules_leg.get("exit"):
                individually_managed.append(idx)
            else:
                combined_managed.append(idx)

        leg_exit_date: dict[int, str] = {}
        leg_exit_reason: dict[int, str] = {}

        # --- Individually-managed legs: each walked independently against
        # its OWN exit config and OWN contract's trading days. ---
        for idx in individually_managed:
            leg = legs[idx]
            own_exit = self.rules["legs"][idx].get("exit") or {}
            take_profit_pct = own_exit.get("take_profit_pct")
            stop_loss_pct = own_exit.get("stop_loss_pct")
            trailing = own_exit.get("trailing") or {}
            trailing_enabled = bool(trailing.get("enabled"))
            natural = leg["_natural_exit_date"]

            exit_day, reason = natural, "EXPIRY"
            if take_profit_pct is not None or stop_loss_pct is not None or trailing_enabled:
                highest_price = lowest_price = current_stop_price = None
                if trailing_enabled:
                    highest_price, lowest_price, current_stop_price, _ = advance_trailing_stop(
                        leg["transaction_type"], leg["entry_price"], trailing["trail_amount"],
                        trailing["trail_type"], None, None, None,
                    )
                for day in self._trading_days(leg["expiry"], entry_date, natural):
                    self.feed.set_time(datetime.combine(date.fromisoformat(day), _MARKET_OPEN))
                    now = self.feed.get_ltp(leg["instrument_token"])
                    if now is None:
                        continue
                    triggered = False
                    if trailing_enabled:
                        highest_price, lowest_price, current_stop_price, _ = advance_trailing_stop(
                            leg["transaction_type"], now, trailing["trail_amount"], trailing["trail_type"],
                            highest_price, lowest_price, current_stop_price,
                        )
                        if stop_triggered(leg["transaction_type"], now, current_stop_price):
                            exit_day, reason, triggered = day, "TRAILING_STOP", True
                    if not triggered and (take_profit_pct is not None or stop_loss_pct is not None):
                        pnl_pct = self._leg_pnl_pct(leg, now)
                        if pnl_pct is not None:
                            trigger = check_exit_trigger(pnl_pct, take_profit_pct, stop_loss_pct)
                            if trigger:
                                exit_day, reason, triggered = day, trigger, True
                    if triggered:
                        break
            leg_exit_date[idx] = exit_day
            leg_exit_reason[idx] = reason

        # --- Combined-managed legs: today's single strategy-level TP/SL/
        # exit_days_before_expiry check, walked over the EARLIEST-expiring
        # combined leg's own contract days (can't hold any leg past its
        # own expiry) — for a single-expiry strategy this is every leg,
        # one shared expiry, byte-identical to the old behavior. ---
        if combined_managed:
            combined_legs = [legs[i] for i in combined_managed]
            earliest_leg = min(combined_legs, key=lambda leg: leg["_natural_exit_date"])
            min_natural = earliest_leg["_natural_exit_date"]
            combined_exit_day, combined_exit_reason = min_natural, "EXPIRY"

            if strategy_take_profit_pct is not None or strategy_stop_loss_pct is not None or strategy_exit_days_before_expiry:
                combined_tokens = [leg["instrument_token"] for leg in combined_legs]
                expiry_date_obj = date.fromisoformat(earliest_leg["expiry"])
                for day in self._trading_days(earliest_leg["expiry"], entry_date, min_natural):
                    self.feed.set_time(datetime.combine(date.fromisoformat(day), _MARKET_OPEN))
                    now_prices = {tok: self.feed.get_ltp(tok) for tok in combined_tokens}
                    pnl_pct = self._combined_pnl_pct(combined_legs, now_prices)
                    if pnl_pct is None:
                        continue
                    trigger = check_exit_trigger(pnl_pct, strategy_take_profit_pct, strategy_stop_loss_pct)
                    if trigger is None and strategy_exit_days_before_expiry and is_within_pre_expiry_buffer(
                        date.fromisoformat(day), expiry_date_obj, strategy_exit_days_before_expiry
                    ):
                        trigger = "EXPIRY"
                    if trigger:
                        combined_exit_day, combined_exit_reason = day, trigger
                        break

            for idx in combined_managed:
                leg = legs[idx]
                # A later-expiring combined leg can't be held past ITS OWN
                # natural expiry even if the group trigger hasn't fired yet.
                if combined_exit_day <= leg["_natural_exit_date"]:
                    leg_exit_date[idx] = combined_exit_day
                    leg_exit_reason[idx] = combined_exit_reason
                else:
                    leg_exit_date[idx] = leg["_natural_exit_date"]
                    leg_exit_reason[idx] = "EXPIRY"

        gross_pnl = 0.0
        net_pnl = 0.0
        leg_charges = []
        liquid = True
        for idx, leg in enumerate(legs):
            exit_day = leg_exit_date[idx]
            self.feed.set_time(datetime.combine(date.fromisoformat(exit_day), _MARKET_OPEN))
            token = leg["instrument_token"]
            exit_transaction_type = "BUY" if leg["transaction_type"] == "SELL" else "SELL"
            if leg["instrument_type"] == "OPTION":
                raw_ltp = self.feed.get_ltp(token)
                if raw_ltp is None:
                    log.warning("Cycle entry=%s: no exit price for %s on %s — skipping cycle.", entry_date, token, exit_day)
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
                entry_costs = calculate_options_transaction_cost_breakdown(leg["entry_price"], leg["quantity"], leg["transaction_type"], self.charge_rates)
                exit_costs = calculate_options_transaction_cost_breakdown(exit_price, leg["quantity"], exit_transaction_type, self.charge_rates)
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
            leg["exit_date"] = exit_day
            leg["exit_reason"] = leg_exit_reason[idx]

        entry_premium_total = sum(leg["entry_price"] * leg["quantity"] for leg in legs)
        pnl_pct_of_premium = (net_pnl / entry_premium_total * 100.0) if entry_premium_total > 0 else 0.0
        charges_total = sum_breakdowns(*leg_charges).get("total", 0) if leg_charges else 0.0

        all_exit_dates = [leg_exit_date[idx] for idx in range(len(legs))]
        all_reasons = {leg_exit_reason[idx] for idx in range(len(legs))}
        overall_exit_date = max(all_exit_dates)
        overall_exit_reason = next(iter(all_reasons)) if len(all_reasons) == 1 else "MIXED"

        return {
            "entry_date": entry_date,
            "expiry": legs[0].get("expiry") if legs else None,
            "exit_date": overall_exit_date,
            "exit_reason": overall_exit_reason,
            "spot_at_entry": result["spot_price"],
            "legs": [
                {"instrument_type": leg["instrument_type"], "option_type": leg["option_type"], "strike": leg["strike"],
                 "transaction_type": leg["transaction_type"], "quantity": leg["quantity"], "entry_price": leg["entry_price"],
                 "expiry": leg.get("expiry"), "exit_date": leg.get("exit_date"), "exit_reason": leg.get("exit_reason"),
                 "exit_price": leg.get("exit_price"), "exit_order_id": leg.get("exit_order_id"),
                 "greeks_at_entry": leg.get("greeks_at_entry")}
                for leg in legs
            ],
            "gross_pnl": round(gross_pnl, 2),
            "charges": round(charges_total, 2),
            "net_pnl": round(net_pnl, 2),
            "pnl_pct_of_premium": round(pnl_pct_of_premium, 2),
            "won": net_pnl > 0,
            "liquid": liquid,
        }
