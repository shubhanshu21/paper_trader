"""
strategies/custom/intraday_indicator_strategy.py — executor for the
Supertrend + Pivot Point intraday option-selling strategy (see
intraday_schema.py for the rules contract, and
api/intraday_indicator_scheduler.py for the scheduler that owns entry/
exit TIMING and open-position state — this class only knows how to read
a signal and place/report one order, never when to call either).

Unlike RuleBasedStrategy (rule_schema.py), this doesn't hold a fixed set
of legs — it holds ONE dynamic leg whose option_type is decided fresh
each time evaluate_signal() is called, from real closed candles and a
real previous-session pivot, never guessed or simulated.
"""
from datetime import date

from automate.broker.base_broker import BaseBroker
from automate.compliance.sebi_rules import AuditTrail, ComplianceError, KillSwitch, OrderRateLimiter, validate_order_quantity, validate_price_band
from automate.strategies.custom.intraday_schema import get_setting
from automate.utils.logger import get_logger
from automate.utils.option_utils import find_instrument_token, find_nearest_expiry_by_type, round_to_nearest_strike
from automate.utils.technical_indicators import pivot_points, supertrend

log = get_logger(__name__)


class IntradaySupertrendStrategy:
    """
    Args mirror RuleBasedStrategy's where they overlap (broker/audit/
    kill_switch/rate_limiter/symbol/rules/user_id) plus:

        product: defaults to "MIS" (intraday margin), not "NRML" — this
            strategy never holds anything overnight by design (see
            intraday_schema.py's exit_time), so MIS is both the cheaper
            margin product and a broker-side safety net (auto square-off)
            if this app's own exit_time check were ever somehow missed.
    """

    def __init__(
        self,
        broker: BaseBroker,
        audit: AuditTrail,
        kill_switch: KillSwitch,
        rate_limiter: OrderRateLimiter,
        symbol: str,
        rules: dict,
        strike_step: float | None = None,
        product: str = "MIS",
        user_id: int | None = None,
    ) -> None:
        self.broker = broker
        self.audit = audit
        self.kill_switch = kill_switch
        self.rate_limiter = rate_limiter
        self.symbol = symbol.upper()
        self.rules = rules
        self.product = product.upper()
        self.user_id = user_id
        self.instrument_key = broker.resolve_instrument_key(self.symbol)

        real_step = strike_step if strike_step is not None else broker.get_strike_step(self.symbol)
        if real_step is None:
            raise RuntimeError(f"Could not resolve a real strike step for '{self.symbol}' — refusing to guess.")
        self.strike_step = real_step

        real_lot_size = broker.get_lot_size(self.symbol)
        if real_lot_size is None:
            raise RuntimeError(
                f"Could not resolve a real lot size for '{self.symbol}' from {type(broker).__name__}'s "
                f"instrument master — refusing to guess (wrong quantity is a real-money risk)."
            )
        self.real_lot_size = real_lot_size

    def evaluate_signal(self) -> dict | None:
        """
        Fetch real closed candles for TODAY's session + the previous
        session's daily OHLC, compute Supertrend + pivot R1/S1, and
        return the latest CLOSED candle's read:

            {"trend": 1 | -1, "supertrend_value": float, "close": float,
             "timestamp": str, "r1": float, "s1": float,
             "signal": "BULLISH" | "BEARISH" | "NONE"}

        Returns None if there isn't yet enough of TODAY's candle history
        for a real Supertrend read, or the broker couldn't supply
        candles/the previous session's OHLC — never a guessed or
        partial-data signal (a caller treating None as "no action this
        tick" is always the safe default here).
        """
        interval = get_setting(self.rules, "candle_interval_minutes")
        period = get_setting(self.rules, "supertrend_period")
        multiplier = get_setting(self.rules, "supertrend_multiplier")
        today_str = date.today().isoformat()

        raw = self.broker.get_historical_candles(self.instrument_key, "minutes", interval, today_str)
        if not raw:
            return None
        candles = sorted(raw, key=lambda c: c["timestamp"])
        # Only TODAY's own candles go into the Supertrend window —
        # blending in a prior session's candles would mix two different
        # volatility regimes into one ATR, which isn't what "Supertrend
        # on the 5-min intraday chart" means to a trader reading it live.
        today_candles = [c for c in candles if c["timestamp"].startswith(today_str)]
        if len(today_candles) < period + 1:
            return None

        st = supertrend(today_candles, period=period, multiplier=multiplier)
        latest = st[-1]
        if latest is None:
            return None

        daily_raw = self.broker.get_historical_candles(self.instrument_key, "days", 1, today_str)
        if not daily_raw or len(daily_raw) < 2:
            return None
        # Most-recent-first per BaseBroker.get_historical_candles' own
        # contract — index 0 is TODAY (partial/in-progress), index 1 is
        # the previous COMPLETE session, exactly what pivot points need.
        daily_sorted = sorted(daily_raw, key=lambda c: c["timestamp"], reverse=True)
        prev_session = daily_sorted[1]
        pivots = pivot_points(prev_session["high"], prev_session["low"], prev_session["close"])

        close = today_candles[-1]["close"]
        signal = "NONE"
        if close > latest["value"] and close > pivots["r1"]:
            signal = "BULLISH"
        elif close < latest["value"] and close < pivots["s1"]:
            signal = "BEARISH"

        return {
            "trend": latest["trend"], "supertrend_value": latest["value"],
            "close": close, "timestamp": today_candles[-1]["timestamp"],
            "r1": pivots["r1"], "s1": pivots["s1"], "signal": signal,
        }

    def resolve_atm_leg(self, option_type: str) -> dict:
        """
        Resolve the nearest-WEEKLY ATM contract for `option_type`
        ("CE"/"PE") — weekly (not monthly): highest premium/Delta
        responsiveness and fastest Theta decay per the reference
        strategy's own stated rationale, the same weekly-by-default
        convention every other strategy in this builder already uses
        (rule_schema.py's expiry.mode). Runs the same pre-trade price-
        band/freeze-quantity compliance checks as every other order path
        in this app (compliance/sebi_rules.py) — never skipped just
        because this strategy's entry timing is signal-driven instead of
        a fixed clock time.
        """
        spot = self.broker.get_ltp(self.instrument_key)
        if spot is None or spot <= 0:
            raise RuntimeError(f"Invalid/missing LTP for '{self.symbol}'.")

        expiries = self.broker.get_option_contracts(self.instrument_key)
        if not expiries:
            raise RuntimeError(f"No option expiries returned for '{self.symbol}'.")
        now = self.broker.get_current_time()
        expiry = find_nearest_expiry_by_type(expiries, "WEEKLY", reference_date=now.date() if now else None)
        if not expiry:
            raise RuntimeError(f"Could not determine nearest weekly expiry for '{self.symbol}'.")

        chain = self.broker.get_option_chain(self.instrument_key, expiry)
        if not chain:
            raise RuntimeError(f"Empty option chain for {self.symbol} expiry {expiry}.")

        strike = round_to_nearest_strike(spot, self.strike_step)
        token = find_instrument_token(chain, strike, option_type)
        if not token:
            raise ComplianceError(f"No listed contract for {self.symbol} {strike} {option_type} expiry {expiry}.")

        lots = self.rules.get("lots", 1)
        quantity = lots * self.real_lot_size

        try:
            validate_price_band(strike, spot)
        except ValueError as exc:
            raise ComplianceError(str(exc)) from exc
        validate_order_quantity(self.symbol, quantity)

        return {
            "instrument_token": token, "strike": strike, "option_type": option_type,
            "expiry": expiry, "quantity": quantity, "spot_at_entry": spot,
        }

    def enter(self, option_type: str) -> dict:
        """
        SELL the ATM leg for `option_type`. Returns the resolved+filled
        leg dict (instrument_token/strike/option_type/expiry/quantity/
        order_id/entry_price/transaction_type) on success. Raises on a
        hard failure (no listed contract, compliance rejection, or the
        order itself failing to place) — the scheduler decides how to
        log/notify/retry, same division of responsibility
        RuleBasedStrategy.execute() has with its own caller.
        """
        leg = self.resolve_atm_leg(option_type)
        self.rate_limiter.acquire()
        self.audit.record(
            event_type="ORDER_INITIATED", symbol=self.symbol, instrument_token=leg["instrument_token"],
            option_type=option_type, strike=leg["strike"], quantity=leg["quantity"], status="PENDING",
            note="intraday_supertrend_entry",
        )
        order_id = self.broker.place_sell_order(
            instrument_token=leg["instrument_token"], quantity=leg["quantity"], product=self.product,
            order_type="MARKET", tag=f"INTRADAY_{option_type}_{self.symbol[:6]}"[:20], user_id=self.user_id,
        )
        if not self.broker.dry_run and not order_id:
            self.audit.record(
                event_type="ORDER_FAILED", symbol=self.symbol, instrument_token=leg["instrument_token"],
                option_type=option_type, strike=leg["strike"], quantity=leg["quantity"], status="FAILED",
            )
            raise RuntimeError(f"{self.symbol}: SELL {option_type} order failed to place.")

        fill_price = self.broker.get_fill_price(order_id) if order_id else None
        entry_price = fill_price if fill_price is not None else self.broker.get_ltp(leg["instrument_token"])
        self.audit.record(
            event_type="ORDER_PLACED", symbol=self.symbol, instrument_token=leg["instrument_token"],
            option_type=option_type, strike=leg["strike"], quantity=leg["quantity"],
            order_id=order_id or "DRY_RUN", status="PLACED",
        )
        leg["order_id"] = order_id
        leg["entry_price"] = entry_price
        leg["transaction_type"] = "SELL"
        return leg
