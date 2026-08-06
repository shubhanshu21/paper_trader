"""
strategies/custom/macd_credit_strategy.py — executor for the MACD-based
overnight directional credit-spread "Stop & Reverse" strategy (see
macd_credit_schema.py for the rules contract and why this needed its own
engine).

One 2-leg credit spread open at a time, ALWAYS one direction or the
other once started (see api/macd_credit_engine.py for the reverse-on-
flip tick logic — this class only knows how to read the trend and place/
close spreads, not when to do so).
"""
from datetime import date

from broker.base_broker import BaseBroker
from compliance.sebi_rules import (
    AuditTrail,
    ComplianceError,
    KillSwitch,
    OrderRateLimiter,
    validate_order_quantity,
)
from strategies.custom.macd_credit_schema import get_setting
from utils.logger import get_logger
from utils.option_utils import (
    find_expiry_by_type_offset,
    find_instrument_token,
    find_nearest_expiry_by_type,
    round_to_nearest_strike,
)
from utils.technical_indicators import macd

log = get_logger(__name__)

_PRODUCT = "NRML"  # overnight/swing, never MIS


class MacdCreditStrategy:
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

    def read_trend(self) -> str | None:
        """
        "BULLISH" (MACD line above its signal line) or "BEARISH" (below),
        off the latest CLOSED 1-hour candle — None if there isn't enough
        history yet to compute a real MACD value.
        """
        candles = self.broker.get_historical_candles(self.instrument_key, "hours", 1, date.today().isoformat())
        if not candles:
            raise RuntimeError(f"{self.symbol}: no hourly candles available.")
        chron = list(reversed(candles))  # broker returns most-recent-first; macd() needs oldest-first
        values = macd(chron, get_setting(self.rules, "macd_fast"), get_setting(self.rules, "macd_slow"), get_setting(self.rules, "macd_signal"))
        latest = values[-1]
        if latest is None:
            return None
        return "BULLISH" if latest["macd"] > latest["signal"] else "BEARISH"

    def resolve_expiry(self) -> str:
        """Current month's nearest expiry if today is on/before rollover_day_of_month, otherwise next month's — see macd_credit_schema.py."""
        expiries = self.broker.get_option_contracts(self.instrument_key)
        if not expiries:
            raise RuntimeError(f"{self.symbol}: no option expiries returned.")
        now = self.broker.get_current_time()
        today = now.date() if now else date.today()
        rollover_day = get_setting(self.rules, "rollover_day_of_month")
        if today.day <= rollover_day:
            expiry = find_nearest_expiry_by_type(expiries, "MONTHLY", reference_date=today)
        else:
            expiry = find_expiry_by_type_offset(expiries, "MONTHLY", 1, reference_date=today)
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

    # ------------------------------------------------------------------

    def _premium_at(self, chain: list, strike: float, option_type: str) -> float | None:
        token = find_instrument_token(chain, strike, option_type)
        if not token:
            return None
        return self.broker.get_ltp(token)

    def find_credit_spread(self, chain: list, option_type: str, direction_sign: int, atm_strike: float) -> tuple[float, float]:
        """
        (short_strike, hedge_strike) whose net credit (short premium -
        hedge premium) falls in [credit_min, credit_max]. Exploits the
        fact that, for a FIXED short strike, credit rises monotonically
        as the hedge moves further away (hedge premium shrinks toward 0)
        — so for each short-strike candidate (richest/nearest-ATM first)
        we scan the hedge distance outward and stop at the first hedge
        whose credit lands in the target band, rather than a blind O(n²)
        search over every (short, hedge) pair.
        """
        max_steps = get_setting(self.rules, "credit_search_max_steps")
        credit_min, credit_max = get_setting(self.rules, "credit_min"), get_setting(self.rules, "credit_max")

        for short_step in range(1, max_steps + 1):
            short_strike = round_to_nearest_strike(atm_strike + direction_sign * short_step * self.strike_step, self.strike_step)
            short_premium = self._premium_at(chain, short_strike, option_type)
            if short_premium is None or short_premium < credit_min:
                continue  # even a near-zero-premium hedge can't reach credit_min off this short leg
            for hedge_step in range(short_step + 1, short_step + max_steps + 1):
                hedge_strike = round_to_nearest_strike(atm_strike + direction_sign * hedge_step * self.strike_step, self.strike_step)
                hedge_premium = self._premium_at(chain, hedge_strike, option_type)
                if hedge_premium is None:
                    continue
                credit = short_premium - hedge_premium
                if credit < credit_min:
                    continue  # spread still too narrow — widen further (credit grows as the hedge premium shrinks)
                if credit <= credit_max:
                    return short_strike, hedge_strike
                break  # jumped straight past credit_max without ever landing in the band (coarse strike grid) — try a different short strike
        raise ComplianceError(f"{self.symbol}: no (short, hedge) {option_type} strike pair within {max_steps} steps of ATM {atm_strike} has a net credit between ₹{credit_min} and ₹{credit_max} — refusing to guess.")

    def _resolve_token(self, chain: list, strike: float, option_type: str) -> str:
        token = find_instrument_token(chain, strike, option_type)
        if not token:
            raise ComplianceError(f"No listed contract for {self.symbol} {strike} {option_type}.")
        return token

    def _place(self, action: str, instrument_token: str, quantity: int, strike: float, option_type: str, tag: str) -> dict:
        self.rate_limiter.acquire()
        place = self.broker.place_sell_order if action == "SELL" else self.broker.place_buy_order
        self.audit.record(
            event_type="ORDER_INITIATED", symbol=self.symbol, instrument_token=instrument_token,
            option_type=option_type, strike=strike, quantity=quantity, status="PENDING", note=f"macd_credit action={action}",
        )
        order_id = place(instrument_token=instrument_token, quantity=quantity, product=_PRODUCT, order_type="MARKET", tag=tag[:20], user_id=self.user_id)
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

    def enter(self, expiry: str, trend: str) -> tuple[dict, dict]:
        """Bull Put Spread (BULLISH) or Bear Call Spread (BEARISH). Returns (short_leg, hedge_leg)."""
        option_type = "PE" if trend == "BULLISH" else "CE"
        direction_sign = -1 if trend == "BULLISH" else 1  # PE spread sits below spot, CE spread above
        atm_strike = self.get_atm_strike()
        chain = self.get_chain(expiry)
        short_strike, hedge_strike = self.find_credit_spread(chain, option_type, direction_sign, atm_strike)
        quantity = self._quantity()

        short_token = self._resolve_token(chain, short_strike, option_type)
        hedge_token = self._resolve_token(chain, hedge_strike, option_type)
        short_fill = self._place("SELL", short_token, quantity, short_strike, option_type, f"MACDCR_{self.symbol[:4]}_SHORT")
        hedge_fill = self._place("BUY", hedge_token, quantity, hedge_strike, option_type, f"MACDCR_{self.symbol[:4]}_HEDGE")

        short_leg = {"instrument_token": short_token, "strike": short_strike, "expiry": expiry, "option_type": option_type, "quantity": quantity, "entry_price": short_fill["price"], "order_id": short_fill["order_id"], "transaction_type": "SELL", "role": "SHORT"}
        hedge_leg = {"instrument_token": hedge_token, "strike": hedge_strike, "expiry": expiry, "option_type": option_type, "quantity": quantity, "entry_price": hedge_fill["price"], "order_id": hedge_fill["order_id"], "transaction_type": "BUY", "role": "HEDGE"}
        return short_leg, hedge_leg

    def close_leg(self, instrument_token: str, quantity: int, strike: float, option_type: str, original_transaction_type: str) -> dict:
        action = "BUY" if original_transaction_type == "SELL" else "SELL"
        fill = self._place(action, instrument_token, quantity, strike, option_type, f"MACDCR_{self.symbol[:4]}_CLOSE")
        return {"exit_price": fill["price"], "order_id": fill["order_id"]}
