"""
strategies/custom/zero_to_hero_strategy.py — executor for the "Zero to
Hero" PDH/PDL breakout-pullback option BUYING strategy (see
zero_to_hero_schema.py for the rules contract, and
api/zero_to_hero_engine.py for the scheduler that owns entry/exit TIMING
and open-position state — this class only knows how to read the day's
levels/candles, detect ONE live entry pattern, and place/report a BUY
order; never when to call either).

This is the only strategy in this app that BUYS an option to OPEN a
position (going long premium on a directional break) rather than
selling — see BaseBroker.place_buy_order's own docstring, which
documents the convention every other engine here follows ("never used
for opening a new long position"). Mechanically nothing special is
needed to break that convention safely: PaperBroker._place_order and
UpstoxBroker already price an F&O BUY as a plain premium debit (not a
margin block), and utils.pnl.compute_basket_pnl / api.live_positions
already sign a BUY leg's P&L correctly (profit when exit > entry) —
this class is just the first caller to actually use place_buy_order
that way.
"""
from datetime import date

from broker.base_broker import BaseBroker
from compliance.sebi_rules import (
    AuditTrail,
    ComplianceError,
    KillSwitch,
    OrderRateLimiter,
    assert_kill_switch_not_active,
    validate_order_quantity,
    validate_price_band,
)
from strategies.custom.zero_to_hero_schema import get_setting
from utils.logger import get_logger
from utils.option_utils import (
    find_expiry_by_type_offset,
    find_instrument_token,
    round_to_nearest_strike,
)

log = get_logger(__name__)


class ZeroToHeroStrategy:
    """Args mirror IntradaySupertrendStrategy's (broker/audit/kill_switch/rate_limiter/symbol/rules/user_id) — see that class for why each is threaded through, unchanged here."""

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

    def _today_str(self) -> str:
        """
        'YYYY-MM-DD' for whatever the broker considers "now" — real
        wall-clock date.today() for live/paper (BaseBroker.get_current_time()
        returns None there, matching its own documented contract), but the
        SIMULATED date for a backtest replay (MockBroker.get_current_time()
        returns the data feed's current_time). Same broker-time convention
        resolve_leg() below already uses for expiry resolution — this
        method just extends it to PDH/PDL and the intraday candle scan too,
        which previously called date.today() directly and would silently
        read the real date instead of whatever historical day is being
        replayed.
        """
        now = self.broker.get_current_time()
        return (now.date() if now else date.today()).isoformat()

    def get_previous_day_levels(self) -> dict | None:
        """
        {"pdh": float, "pdl": float} off the last COMPLETE daily candle —
        strictly the immediate previous session, per the reference
        strategy's own "ignore older historical levels" rule (never a
        multi-day high/low). Returns None if the broker can't supply at
        least today (partial) + one prior complete session.
        """
        today_str = self._today_str()
        daily_raw = self.broker.get_historical_candles(self.instrument_key, "days", 1, today_str)
        if not daily_raw or len(daily_raw) < 2:
            return None
        # Most-recent-first per BaseBroker.get_historical_candles' own
        # contract — index 0 is TODAY (partial/in-progress), index 1 is
        # the previous COMPLETE session.
        daily_sorted = sorted(daily_raw, key=lambda c: c["timestamp"], reverse=True)
        prev_session = daily_sorted[1]
        return {"pdh": prev_session["high"], "pdl": prev_session["low"]}

    def evaluate_signal(self) -> dict | None:
        """
        Fetch real closed candles for TODAY's session, establish bias off
        PDH/PDL, and look for a fresh pullback-then-confirm entry off the
        MOST RECENTLY CLOSED candle only (never a stale historical match —
        same "act on the tail, not the whole day's history" discipline
        api/intraday_indicator_scheduler.py's Supertrend read uses).

        Returns None if there isn't enough of today's candle history yet,
        or PDH/PDL can't be resolved — never a guessed/partial-data
        signal. Returns {"signal": "NONE"} when price is inside the
        PDH/PDL no-trade zone or no valid pattern has just completed.
        Returns {"signal": "BULLISH"|"BEARISH", "option_type": "CE"|"PE",
        "entry_index_price": float, "sl_index_price": float,
        "target_index_price": float, "trigger_timestamp": str} the
        instant a valid entry candle has just closed.
        """
        levels = self.get_previous_day_levels()
        if levels is None:
            return None
        pdh, pdl = levels["pdh"], levels["pdl"]

        interval = get_setting(self.rules, "candle_interval_minutes")
        today_str = self._today_str()
        raw = self.broker.get_historical_candles(self.instrument_key, "minutes", interval, today_str)
        if not raw:
            return None
        candles = sorted(raw, key=lambda c: c["timestamp"])
        today_candles = [c for c in candles if c["timestamp"].startswith(today_str)]
        if len(today_candles) < 2:
            return None

        close = today_candles[-1]["close"]
        if close > pdh:
            bias = "BULLISH"
        elif close < pdl:
            bias = "BEARISH"
        else:
            return {"signal": "NONE"}

        # Only candles AFTER the bias level was actually crossed count
        # towards the pullback pattern — a pre-breakout green/red candle
        # sitting inside the no-trade zone is noise, not part of this
        # setup.
        cross_idx = None
        for i, c in enumerate(today_candles):
            if bias == "BULLISH" and c["close"] > pdh:
                cross_idx = i
                break
            if bias == "BEARISH" and c["close"] < pdl:
                cross_idx = i
                break
        if cross_idx is None:
            return {"signal": "NONE"}
        post_cross = today_candles[cross_idx:]

        pattern = _detect_entry_pattern(post_cross, bias, get_setting(self.rules, "max_pullback_candles"))
        if pattern is None:
            return {"signal": "NONE"}

        buffer = get_setting(self.rules, "sl_buffer_points")
        entry_candle = pattern["entry_candle"]
        option_type = "PE" if bias == "BEARISH" else "CE"
        entry_index_price = self.broker.get_ltp(self.instrument_key) or entry_candle["close"]
        if bias == "BEARISH":
            sl_index_price = entry_candle["high"] + buffer
            risk = sl_index_price - entry_index_price
            target_index_price = entry_index_price - risk
        else:
            sl_index_price = entry_candle["low"] - buffer
            risk = entry_index_price - sl_index_price
            target_index_price = entry_index_price + risk

        return {
            "signal": bias, "option_type": option_type,
            "entry_index_price": entry_index_price, "sl_index_price": sl_index_price,
            "target_index_price": target_index_price, "trigger_timestamp": entry_candle["timestamp"],
        }

    def resolve_leg(self, option_type: str, quantity: int) -> dict:
        """
        ATM `option_type` contract on the nearest (offset-adjusted)
        weekly expiry — runs the same pre-trade price-band/freeze-
        quantity compliance checks as every other order path in this
        app, never skipped just because this strategy's entry timing is
        signal-driven instead of a fixed clock time.
        """
        spot = self.broker.get_ltp(self.instrument_key)
        if spot is None or spot <= 0:
            raise RuntimeError(f"Invalid/missing LTP for '{self.symbol}'.")

        expiries = self.broker.get_option_contracts(self.instrument_key)
        if not expiries:
            raise RuntimeError(f"No option expiries returned for '{self.symbol}'.")
        now = self.broker.get_current_time()
        expiry = find_expiry_by_type_offset(
            expiries, "WEEKLY", offset=get_setting(self.rules, "expiry_offset"),
            reference_date=now.date() if now else None,
        )
        if not expiry:
            raise RuntimeError(f"Could not determine weekly expiry for '{self.symbol}'.")

        chain = self.broker.get_option_chain(self.instrument_key, expiry)
        if not chain:
            raise RuntimeError(f"Empty option chain for {self.symbol} expiry {expiry}.")

        strike = round_to_nearest_strike(spot, self.strike_step)
        token = find_instrument_token(chain, strike, option_type)
        if not token:
            raise ComplianceError(f"No listed contract for {self.symbol} {strike} {option_type} expiry {expiry}.")

        try:
            validate_price_band(strike, spot)
        except ValueError as exc:
            raise ComplianceError(str(exc)) from exc
        validate_order_quantity(self.symbol, quantity)

        return {"instrument_token": token, "strike": strike, "option_type": option_type, "expiry": expiry, "quantity": quantity}

    def enter(self, option_type: str, quantity: int) -> dict:
        """
        BUY `quantity` of the ATM `option_type` leg — going LONG premium
        (see this module's docstring for why BUY-to-open is safe here).
        Returns the resolved+filled leg dict on success. Raises on a
        hard failure — the scheduler decides how to log/notify/retry,
        same division of responsibility every other engine's enter()
        has with its own caller.
        """
        leg = self.resolve_leg(option_type, quantity)
        assert_kill_switch_not_active(self.kill_switch)  # entry only — exiting an already-open leg must still work even while halted
        self.rate_limiter.acquire()
        self.audit.record(
            event_type="ORDER_INITIATED", symbol=self.symbol, instrument_token=leg["instrument_token"],
            option_type=option_type, strike=leg["strike"], quantity=leg["quantity"], status="PENDING",
            note="zero_to_hero_entry",
        )
        order_id = self.broker.place_buy_order(
            instrument_token=leg["instrument_token"], quantity=leg["quantity"], product=self.product,
            order_type="MARKET", tag=f"Z2H_{option_type}_{self.symbol[:6]}"[:20], user_id=self.user_id,
        )
        if not self.broker.dry_run and not order_id:
            self.audit.record(
                event_type="ORDER_FAILED", symbol=self.symbol, instrument_token=leg["instrument_token"],
                option_type=option_type, strike=leg["strike"], quantity=leg["quantity"], status="FAILED",
            )
            raise RuntimeError(f"{self.symbol}: BUY {option_type} order failed to place.")

        fill_price = self.broker.get_fill_price(order_id) if order_id else None
        entry_price = fill_price if fill_price is not None else self.broker.get_ltp(leg["instrument_token"])
        self.audit.record(
            event_type="ORDER_PLACED", symbol=self.symbol, instrument_token=leg["instrument_token"],
            option_type=option_type, strike=leg["strike"], quantity=leg["quantity"],
            order_id=order_id or "DRY_RUN", status="PLACED",
        )
        leg["order_id"] = order_id
        leg["entry_price"] = entry_price
        leg["transaction_type"] = "BUY"
        return leg


def _detect_entry_pattern(candles: list[dict], bias: str, max_pullback: int) -> dict | None:
    """
    Pure pattern check against the TAIL of `candles` (already filtered to
    "today, at/after the PDH/PDL cross") — only ever looks at the run
    immediately preceding the LAST candle, so a pattern from earlier in
    the day that was never acted on can't fire retroactively once
    conditions happen to line up again later.

    BEARISH (PE): counter-color = green (buyers' pullback attempt),
    confirm-color = red. BULLISH (CE): counter-color = red, confirm-color
    = green. A confirm candle closing beyond the OPEN of the counter
    candle immediately before it is the entry trigger — "beyond" meaning
    below for a red confirm, above for a green one. 1-2 counter candles
    immediately before it is a valid pullback; 3+ cancels the setup
    (treated as a trend reversal, not a pullback) — return None either
    way, same as "no pattern here."

    Returns {"entry_candle": dict, "pullback_len": int} or None.
    """
    if len(candles) < 2:
        return None
    last = candles[-1]
    is_green = last["close"] > last["open"]
    is_red = last["close"] < last["open"]
    if bias == "BEARISH" and not is_red:
        return None
    if bias == "BULLISH" and not is_green:
        return None

    def is_counter(c: dict) -> bool:
        return (c["close"] > c["open"]) if bias == "BEARISH" else (c["close"] < c["open"])

    run = 0
    i = len(candles) - 2
    while i >= 0 and is_counter(candles[i]):
        run += 1
        i -= 1
    if run == 0 or run > max_pullback:
        return None

    reference_open = candles[len(candles) - 2]["open"]  # counter candle immediately before the confirm candle
    if bias == "BEARISH" and not (last["close"] < reference_open):
        return None
    if bias == "BULLISH" and not (last["close"] > reference_open):
        return None

    return {"entry_candle": last, "pullback_len": run}
