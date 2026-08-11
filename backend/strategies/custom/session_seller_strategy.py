"""
strategies/custom/session_seller_strategy.py — executor for the "Intraday
Nifty & Sensex Option Selling" dual-session strategy (see
session_seller_schema.py for the rules contract and why this needed its
own engine).

Genuinely intraday — MIS product, never NRML (see intraday_indicator_
strategy.py for the same convention). Works against whichever symbol
(NIFTY or SENSEX) the caller passes in for THIS tick; the engine
(api/session_seller_engine.py) is what decides which one, off the day's
weekday schedule.
"""
from broker.base_broker import BaseBroker
from compliance.sebi_rules import (
    AuditTrail,
    ComplianceError,
    KillSwitch,
    OrderRateLimiter,
    assert_kill_switch_not_active,
    validate_order_quantity,
)
from strategies.custom.session_seller_schema import get_setting
from utils.logger import get_logger
from utils.option_utils import (
    find_instrument_token,
    find_nearest_expiry_by_type,
    round_to_nearest_strike,
)

log = get_logger(__name__)

_PRODUCT = "MIS"
_MAX_HEDGE_SEARCH_STEPS = 60


class SessionSellerStrategy:
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

    def resolve_expiry(self) -> str:
        expiries = self.broker.get_option_contracts(self.instrument_key)
        if not expiries:
            raise RuntimeError(f"{self.symbol}: no option expiries returned.")
        now = self.broker.get_current_time()
        expiry = find_nearest_expiry_by_type(expiries, "WEEKLY", reference_date=now.date() if now else None)
        if not expiry:
            raise RuntimeError(f"{self.symbol}: could not resolve the nearest weekly expiry.")
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

    def find_hedge_strike(self, chain: list, atm_strike: float, option_type: str) -> float:
        """
        Nearest strike, searching outward from ATM in this option_type's
        OTM direction, whose live premium falls within [hedge_premium_min,
        hedge_premium_max] — same "cheapest real listed contract in a
        price band" search rule_strategy.py's PREMIUM_BAND leg resolution
        and smart_condor_strategy.py's resolve_matching_strike() both use.
        """
        band_min, band_max = get_setting(self.rules, "hedge_premium_min"), get_setting(self.rules, "hedge_premium_max")
        sign = 1 if option_type == "CE" else -1
        for step_n in range(1, _MAX_HEDGE_SEARCH_STEPS + 1):
            candidate = round_to_nearest_strike(atm_strike + sign * step_n * self.strike_step, self.strike_step)
            token = find_instrument_token(chain, candidate, option_type)
            if not token:
                continue
            premium = self.broker.get_ltp(token)
            if premium is not None and band_min <= premium <= band_max:
                return candidate
        raise ComplianceError(f"{self.symbol}: no {option_type} strike within {_MAX_HEDGE_SEARCH_STEPS} strikes of ATM {atm_strike} has a live premium between ₹{band_min} and ₹{band_max} — refusing to guess.")

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
            option_type=option_type, strike=strike, quantity=quantity, status="PENDING", note=f"session_seller action={action}",
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

    def _quantity(self) -> int:
        quantity = self.rules.get("lots", 1) * self.real_lot_size
        validate_order_quantity(self.symbol, quantity)
        return quantity

    def buy_hedge(self, chain: list, expiry: str, option_type: str, strike: float) -> dict:
        token = self._resolve_token(chain, strike, option_type)
        quantity = self._quantity()
        fill = self._place("BUY", token, quantity, strike, option_type, f"SESSION_{self.symbol[:4]}_HEDGE")
        return {"instrument_token": token, "strike": strike, "expiry": expiry, "option_type": option_type, "quantity": quantity, "entry_price": fill["price"], "order_id": fill["order_id"], "transaction_type": "BUY"}

    def sell_short(self, chain: list, expiry: str, option_type: str, strike: float) -> dict:
        token = self._resolve_token(chain, strike, option_type)
        quantity = self._quantity()
        fill = self._place("SELL", token, quantity, strike, option_type, f"SESSION_{self.symbol[:4]}_SHORT")
        return {"instrument_token": token, "strike": strike, "expiry": expiry, "option_type": option_type, "quantity": quantity, "entry_price": fill["price"], "order_id": fill["order_id"], "transaction_type": "SELL"}

    def close_leg(self, instrument_token: str, quantity: int, strike: float, option_type: str, original_transaction_type: str) -> dict:
        action = "BUY" if original_transaction_type == "SELL" else "SELL"
        fill = self._place(action, instrument_token, quantity, strike, option_type, f"SESSION_{self.symbol[:4]}_CLOSE", is_close=True)
        return {"exit_price": fill["price"], "order_id": fill["order_id"]}
