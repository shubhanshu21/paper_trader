"""
strategies/custom/otm_put_roll_strategy.py — executor for the far-month
OTM Nifty put-selling strategy with a mechanical roll-down adjustment
(see otm_put_roll_schema.py for the rules contract and why this needed
its own engine).

One SHORT PUT leg at a time. Entry, and every roll, price the SAME way:
resolve the strike from a CANDLE-CLOSE-confirmed underlying price (never
a raw LTP tick — see otm_put_roll_schema.py's candle_interval_minutes),
round to the real listed strike grid, and look the contract up in that
expiry's real option chain — same discipline rule_strategy.py's
resolve_leg_strike/find_instrument_token already established, reused
directly here rather than reimplemented.
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
from strategies.custom.otm_put_roll_schema import get_setting
from utils.logger import get_logger
from utils.option_utils import find_instrument_token, find_monthly_expiries, round_to_nearest_strike

log = get_logger(__name__)

_PRODUCT = "NRML"  # holds for weeks at a time, same as the leg-based engine's default (never MIS)


class OtmPutRollStrategy:
    def __init__(
        self,
        broker: BaseBroker,
        audit: AuditTrail,
        kill_switch: KillSwitch,
        rate_limiter: OrderRateLimiter,
        symbol: str,
        rules: dict,
        user_id: int | None = None,
    ) -> None:
        self.broker = broker
        self.audit = audit
        self.kill_switch = kill_switch
        self.rate_limiter = rate_limiter
        self.symbol = symbol.upper()
        self.rules = rules
        self.user_id = user_id

        self.instrument_key = broker.resolve_instrument_key(self.symbol)
        self.strike_step = broker.get_strike_step(self.symbol)
        if self.strike_step is None:
            raise RuntimeError(f"Could not resolve a real strike step for '{self.symbol}' — refusing to guess.")
        self.real_lot_size = broker.get_lot_size(self.symbol)
        if self.real_lot_size is None:
            raise RuntimeError(f"Could not resolve a real lot size for '{self.symbol}' — refusing to guess.")

    # ------------------------------------------------------------------

    def resolve_far_month_expiry(self) -> str:
        """The expiry `expiry_offset` monthly cycles out (0=nearest, 1=the one after — "far month"). See find_monthly_expiries."""
        expiries = self.broker.get_option_contracts(self.instrument_key)
        if not expiries:
            raise RuntimeError(f"{self.symbol}: no option expiries returned.")
        now = self.broker.get_current_time()
        monthly = find_monthly_expiries(expiries, reference_date=now.date() if now else None)
        offset = get_setting(self.rules, "expiry_offset")
        if offset >= len(monthly):
            raise RuntimeError(f"{self.symbol}: only {len(monthly)} monthly expiry(ies) listed, need index {offset} ('far month') — refusing to guess a further one.")
        return monthly[offset]

    def confirmed_close(self) -> float:
        """
        The latest CLOSED candle's close, at candle_interval_minutes
        granularity — the basis for BOTH the pullback-entry and the
        roll-trigger checks, deliberately never a raw LTP tick, so a
        brief intraday wick that immediately reverts can't fire a false
        signal (see otm_put_roll_schema.py's candle_interval_minutes).
        """
        interval = get_setting(self.rules, "candle_interval_minutes")
        candles = self.broker.get_historical_candles(self.instrument_key, "minutes", interval, date.today().isoformat())
        if not candles:
            raise RuntimeError(f"{self.symbol}: no {interval}-minute candles available.")
        return candles[0]["close"]  # broker contract: most-recent-first

    def recent_high(self) -> float:
        """Max daily HIGH over the last pullback_lookback_days sessions (today's still-forming session included, which only makes the pullback threshold slightly harder to clear — the safe direction)."""
        lookback = get_setting(self.rules, "pullback_lookback_days")
        daily = self.broker.get_historical_candles(self.instrument_key, "days", 1, date.today().isoformat())
        if not daily:
            raise RuntimeError(f"{self.symbol}: no daily candles available for the pullback lookback.")
        window = daily[:lookback]
        return max(c["high"] for c in window)

    def pullback_points(self) -> tuple[float, float]:
        """(points off the recent high, the confirmed close used for both sides of that comparison)."""
        close = self.confirmed_close()
        high = self.recent_high()
        return high - close, close

    # ------------------------------------------------------------------

    def _resolve_pe_token(self, expiry: str, strike: float) -> str:
        chain = self.broker.get_option_chain(self.instrument_key, expiry)
        if not chain:
            raise RuntimeError(f"{self.symbol}: empty option chain for expiry {expiry}.")
        token = find_instrument_token(chain, strike, "PE")
        if not token:
            raise ComplianceError(f"No listed contract for {self.symbol} {strike} PE expiry {expiry}.")
        return token

    def _place(self, action: str, instrument_token: str, quantity: int, strike: float, tag: str, is_close: bool = False) -> dict:
        # Kill switch blocks NEW order flow (entries/rolls) only — never a
        # close, which would strand real open risk with no way to exit
        # while halted. Matches this codebase's existing precedent
        # (rule_strategy.py's run_pre_trade_checks is only ever called on
        # the entry path, never on an exit/unwind).
        if not is_close:
            assert_kill_switch_not_active(self.kill_switch)
        self.rate_limiter.acquire()
        place = self.broker.place_sell_order if action == "SELL" else self.broker.place_buy_order
        self.audit.record(
            event_type="ORDER_INITIATED", symbol=self.symbol, instrument_token=instrument_token,
            option_type="PE", strike=strike, quantity=quantity, status="PENDING", note=f"otm_put_roll action={action}",
        )
        order_id = place(instrument_token=instrument_token, quantity=quantity, product=_PRODUCT, order_type="MARKET", tag=tag[:20], user_id=self.user_id)
        status = "DRY_RUN" if self.broker.dry_run else ("PLACED" if order_id else "FAILED")
        self.audit.record(
            event_type="ORDER_PLACED" if status != "FAILED" else "ORDER_FAILED",
            symbol=self.symbol, instrument_token=instrument_token, option_type="PE", strike=strike,
            quantity=quantity, order_id=order_id or "DRY_RUN", status=status,
        )
        if not self.broker.dry_run and not order_id:
            raise RuntimeError(f"{self.symbol}: {action} PE {strike} order failed to place.")
        fill_price = self.broker.get_fill_price(order_id) if order_id else None
        price = fill_price if fill_price is not None else self.broker.get_ltp(instrument_token)
        return {"order_id": order_id, "price": price}

    def enter(self, expiry: str, confirmed_close: float) -> dict:
        """First leg of a cycle: SELL initial_otm_points below the confirmed close."""
        strike = round_to_nearest_strike(confirmed_close - get_setting(self.rules, "initial_otm_points"), self.strike_step)
        token = self._resolve_pe_token(expiry, strike)
        quantity = self.rules.get("lots", 1) * self.real_lot_size

        try:
            validate_price_band(strike, confirmed_close)
        except ValueError as exc:
            raise ComplianceError(str(exc)) from exc
        validate_order_quantity(self.symbol, quantity)

        fill = self._place("SELL", token, quantity, strike, f"PUTROLL_{self.symbol[:6]}_ENTRY")
        return {"instrument_token": token, "strike": strike, "expiry": expiry, "quantity": quantity, "entry_price": fill["price"], "order_id": fill["order_id"]}

    def roll(self, old_strike: float, expiry: str) -> dict:
        """Close is the CALLER's responsibility (see engine's _tick_one_strategy) — this only opens the new, lower-strike leg."""
        new_strike = round_to_nearest_strike(old_strike - get_setting(self.rules, "roll_points"), self.strike_step)
        token = self._resolve_pe_token(expiry, new_strike)
        quantity = self.rules.get("lots", 1) * self.real_lot_size

        validate_order_quantity(self.symbol, quantity)

        fill = self._place("SELL", token, quantity, new_strike, f"PUTROLL_{self.symbol[:6]}_ROLL")
        return {"instrument_token": token, "strike": new_strike, "expiry": expiry, "quantity": quantity, "entry_price": fill["price"], "order_id": fill["order_id"]}

    def close(self, instrument_token: str, quantity: int, strike: float) -> dict:
        fill = self._place("BUY", instrument_token, quantity, strike, f"PUTROLL_{self.symbol[:6]}_CLOSE", is_close=True)
        return {"exit_price": fill["price"], "order_id": fill["order_id"]}
