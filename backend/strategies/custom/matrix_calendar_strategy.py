"""
strategies/custom/matrix_calendar_strategy.py — executor for the "Matrix
Calendar" zero-adjustment strategy (see matrix_calendar_schema.py for the
rules contract and why this needed its own engine).

6 legs open at once, across TWO expiries:
  - SELL weekly CE + SELL weekly PE (2x lots each), strikes delta-targeted
  - BUY weekly CE hedge + BUY weekly PE hedge (1x lots), fixed points OTM
    from each corresponding sold strike
  - BUY monthly CE + BUY monthly PE (1x lots), at the EXACT SAME strikes
    as the weekly shorts — the calendar legs that make this Vega-positive
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
)
from strategies.custom.matrix_calendar_schema import get_setting
from utils import black76
from utils.instrument_cache import InstrumentCache
from utils.logger import get_logger
from utils.option_utils import (
    find_expiry_by_type_offset,
    find_instrument_token,
    find_nearest_expiry_by_type,
    round_to_nearest_strike,
)

log = get_logger(__name__)

_PRODUCT = "NRML"
_MAX_DELTA_SEARCH_STEPS = 30


class MatrixCalendarStrategy:
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
        self._instrument_cache = InstrumentCache()

    # ------------------------------------------------------------------

    def resolve_weekly_expiry(self) -> str:
        expiries = self.broker.get_option_contracts(self.instrument_key)
        if not expiries:
            raise RuntimeError(f"{self.symbol}: no option expiries returned.")
        now = self.broker.get_current_time()
        reference_date = now.date() if now else None
        offset = get_setting(self.rules, "weekly_expiry_offset")
        if offset:
            expiry = find_expiry_by_type_offset(expiries, "WEEKLY", offset, reference_date=reference_date)
        else:
            expiry = find_nearest_expiry_by_type(expiries, "WEEKLY", reference_date=reference_date)
        if not expiry:
            raise RuntimeError(f"{self.symbol}: could not resolve a weekly expiry.")
        return expiry

    def resolve_monthly_expiry(self) -> str:
        expiries = self.broker.get_option_contracts(self.instrument_key)
        if not expiries:
            raise RuntimeError(f"{self.symbol}: no option expiries returned.")
        now = self.broker.get_current_time()
        expiry = find_nearest_expiry_by_type(expiries, "MONTHLY", reference_date=now.date() if now else None)
        if not expiry:
            raise RuntimeError(f"{self.symbol}: could not resolve a monthly expiry.")
        return expiry

    def get_atm_strike(self) -> float:
        spot = self.broker.get_ltp(self.instrument_key)
        if spot is None:
            raise RuntimeError(f"{self.symbol}: could not fetch spot LTP.")
        return round_to_nearest_strike(spot, self.strike_step)

    def get_chain(self, expiry: str) -> list:
        chain = self.broker.get_option_chain(self.instrument_key, expiry)
        if not chain:
            raise RuntimeError(f"{self.symbol}: empty option chain for expiry {expiry}.")
        return chain

    def get_futures_price(self) -> float:
        """Black-76 needs a forward/futures price, not spot — same resolve_nearest_future_key() -> get_ltp() pattern delta_neutral_strategy.py uses."""
        resolved = self._instrument_cache.resolve_nearest_future_key(self.symbol)
        price = self.broker.get_ltp(resolved[0]) if resolved else None
        if price is None:
            raise RuntimeError(f"{self.symbol}: could not resolve a live futures price for Black-76 delta.")
        return price

    def time_to_expiry_years(self, expiry: str) -> float:
        days = max((date.fromisoformat(expiry) - date.today()).days, 1)
        return days / 365.0

    # ------------------------------------------------------------------

    def _grid_strike(self, atm_strike: float, sign: int, step: int) -> float:
        grid = get_setting(self.rules, "strike_grid")
        return round_to_nearest_strike(atm_strike + sign * step * grid, self.strike_step)

    def find_short_strike_by_delta(self, chain: list, option_type: str, atm_strike: float, expiry: str, futures_price: float) -> float:
        """
        Strike (on strike_grid, not the raw exchange strike_step — the
        video's own "100pt not 50pt for liquidity" note) whose live |delta|
        is closest to short_target_delta — delta falls off monotonically
        moving away from ATM, so the first strike at/below target and its
        immediate predecessor bracket the answer; same pattern as
        delta_neutral_strategy.py's find_strike_by_delta.
        """
        target_delta = get_setting(self.rules, "short_target_delta")
        sign = 1 if option_type == "CE" else -1
        years_to_expiry = self.time_to_expiry_years(expiry)
        prev_strike: float | None = None
        prev_delta: float | None = None
        for step in range(0, _MAX_DELTA_SEARCH_STEPS + 1):
            candidate = self._grid_strike(atm_strike, sign, step)
            token = find_instrument_token(chain, candidate, option_type)
            if not token:
                continue
            premium = self.broker.get_ltp(token)
            if premium is None:
                continue
            greeks = black76.compute_greeks_from_market_price(
                F=futures_price, K=candidate, T=years_to_expiry, r=black76.DEFAULT_RISK_FREE_RATE,
                market_price=premium, option_type=option_type,
            )
            delta = abs(greeks["delta"]) if greeks else None
            if delta is None:
                continue
            if prev_delta is not None and delta <= target_delta:
                return prev_strike if abs(prev_delta - target_delta) <= abs(delta - target_delta) else candidate
            prev_strike, prev_delta = candidate, delta
        raise ComplianceError(f"{self.symbol}: no {option_type} strike within {_MAX_DELTA_SEARCH_STEPS} grid steps of ATM {atm_strike} has a live delta near {target_delta} — refusing to guess.")

    def compute_hedge_strikes(self, short_ce_strike: float, short_pe_strike: float) -> tuple[float, float]:
        """(weekly hedge CE strike, weekly hedge PE strike) — fixed points OTM past each sold strike, on the real exchange strike grid."""
        hedge_points = get_setting(self.rules, "weekly_hedge_points")
        hedge_ce = round_to_nearest_strike(short_ce_strike + hedge_points, self.strike_step)
        hedge_pe = round_to_nearest_strike(short_pe_strike - hedge_points, self.strike_step)
        return hedge_ce, hedge_pe

    # ------------------------------------------------------------------

    def _resolve_token(self, chain: list, strike: float, option_type: str) -> str:
        token = find_instrument_token(chain, strike, option_type)
        if not token:
            raise ComplianceError(f"No listed contract for {self.symbol} {strike} {option_type}.")
        return token

    def _place(self, action: str, instrument_token: str, quantity: int, strike: float, option_type: str, tag: str, is_close: bool = False) -> dict:
        # Kill switch blocks NEW order flow (entries/adjustments) only —
        # never a close, which would strand real open risk with no way to
        # exit while halted. Matches this codebase's existing precedent
        # (rule_strategy.py's run_pre_trade_checks is only ever called on
        # the entry path, never on an exit/unwind).
        if not is_close:
            assert_kill_switch_not_active(self.kill_switch)
        self.rate_limiter.acquire()
        place = self.broker.place_sell_order if action == "SELL" else self.broker.place_buy_order
        self.audit.record(
            event_type="ORDER_INITIATED", symbol=self.symbol, instrument_token=instrument_token,
            option_type=option_type, strike=strike, quantity=quantity, status="PENDING", note=f"matrix_calendar action={action}",
        )
        order_id = place(instrument_token=instrument_token, quantity=quantity, product=_PRODUCT, order_type="MARKET", tag=tag[:20], user_id=self.user_id, is_close=is_close)
        status = "DRY_RUN" if self.broker.dry_run else ("PLACED" if order_id else "FAILED")
        self.audit.record(
            event_type="ORDER_PLACED" if status != "FAILED" else "ORDER_FAILED",
            symbol=self.symbol, instrument_token=instrument_token, option_type=option_type, strike=strike,
            quantity=quantity, order_id=order_id or "DRY_RUN", status=status,
        )
        if not self.broker.dry_run and not order_id:
            raise RuntimeError(f"{self.symbol}: {action} {option_type} {strike} order failed to place.")
        fill_price = self.broker.get_fill_price(order_id) if order_id else None
        price = fill_price if fill_price is not None else self.broker.get_ltp(instrument_token)
        return {"order_id": order_id, "price": price}

    def _quantity(self, multiplier: int) -> int:
        quantity = self.rules.get("lots", 1) * multiplier * self.real_lot_size
        validate_order_quantity(self.symbol, quantity)
        return quantity

    def enter(self, weekly_expiry: str, monthly_expiry: str) -> list[dict]:
        """All 6 legs of one cycle. Returns leg dicts each tagged with its own `expiry` and a `role`."""
        atm_strike = self.get_atm_strike()
        weekly_chain = self.get_chain(weekly_expiry)
        futures_price = self.get_futures_price()

        short_ce_strike = self.find_short_strike_by_delta(weekly_chain, "CE", atm_strike, weekly_expiry, futures_price)
        short_pe_strike = self.find_short_strike_by_delta(weekly_chain, "PE", atm_strike, weekly_expiry, futures_price)
        hedge_ce_strike, hedge_pe_strike = self.compute_hedge_strikes(short_ce_strike, short_pe_strike)

        monthly_chain = self.get_chain(monthly_expiry)

        short_ce_token = self._resolve_token(weekly_chain, short_ce_strike, "CE")
        short_pe_token = self._resolve_token(weekly_chain, short_pe_strike, "PE")
        hedge_ce_token = self._resolve_token(weekly_chain, hedge_ce_strike, "CE")
        hedge_pe_token = self._resolve_token(weekly_chain, hedge_pe_strike, "PE")
        monthly_ce_token = self._resolve_token(monthly_chain, short_ce_strike, "CE")
        monthly_pe_token = self._resolve_token(monthly_chain, short_pe_strike, "PE")

        short_qty = self._quantity(2)
        hedge_qty = self._quantity(1)

        short_ce_fill = self._place("SELL", short_ce_token, short_qty, short_ce_strike, "CE", f"MTX_{self.symbol[:4]}_SCE")
        short_pe_fill = self._place("SELL", short_pe_token, short_qty, short_pe_strike, "PE", f"MTX_{self.symbol[:4]}_SPE")
        hedge_ce_fill = self._place("BUY", hedge_ce_token, hedge_qty, hedge_ce_strike, "CE", f"MTX_{self.symbol[:4]}_HCE")
        hedge_pe_fill = self._place("BUY", hedge_pe_token, hedge_qty, hedge_pe_strike, "PE", f"MTX_{self.symbol[:4]}_HPE")
        monthly_ce_fill = self._place("BUY", monthly_ce_token, hedge_qty, short_ce_strike, "CE", f"MTX_{self.symbol[:4]}_MCE")
        monthly_pe_fill = self._place("BUY", monthly_pe_token, hedge_qty, short_pe_strike, "PE", f"MTX_{self.symbol[:4]}_MPE")

        def _leg(fill, token, strike, option_type, qty, transaction_type, role, expiry):
            return {
                "instrument_token": token, "strike": strike, "expiry": expiry, "option_type": option_type,
                "quantity": qty, "entry_price": fill["price"], "order_id": fill["order_id"],
                "transaction_type": transaction_type, "role": role,
            }

        return [
            _leg(short_ce_fill, short_ce_token, short_ce_strike, "CE", short_qty, "SELL", "WEEKLY_SHORT", weekly_expiry),
            _leg(short_pe_fill, short_pe_token, short_pe_strike, "PE", short_qty, "SELL", "WEEKLY_SHORT", weekly_expiry),
            _leg(hedge_ce_fill, hedge_ce_token, hedge_ce_strike, "CE", hedge_qty, "BUY", "WEEKLY_HEDGE", weekly_expiry),
            _leg(hedge_pe_fill, hedge_pe_token, hedge_pe_strike, "PE", hedge_qty, "BUY", "WEEKLY_HEDGE", weekly_expiry),
            _leg(monthly_ce_fill, monthly_ce_token, short_ce_strike, "CE", hedge_qty, "BUY", "MONTHLY_CALENDAR", monthly_expiry),
            _leg(monthly_pe_fill, monthly_pe_token, short_pe_strike, "PE", hedge_qty, "BUY", "MONTHLY_CALENDAR", monthly_expiry),
        ]

    def close_leg(self, instrument_token: str, quantity: int, strike: float, option_type: str, original_transaction_type: str) -> dict:
        action = "BUY" if original_transaction_type == "SELL" else "SELL"
        fill = self._place(action, instrument_token, quantity, strike, option_type, f"MTX_{self.symbol[:4]}_CLOSE", is_close=True)
        return {"exit_price": fill["price"], "order_id": fill["order_id"]}
