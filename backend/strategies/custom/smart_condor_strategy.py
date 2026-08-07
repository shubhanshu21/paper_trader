"""
strategies/custom/smart_condor_strategy.py — executor for the "Smart
Condor" weekly iron-condor strategy with a mechanical, capped
premium-ratio adjustment protocol (see smart_condor_schema.py for the
rules contract and why this needed its own engine).

Up to 4 legs open at once (short CE, short PE, long CE hedge, long PE
hedge). Entry strikes are derived from the LIVE ATM straddle premium
(never a hardcoded points offset — that's what keeps the condor's width
proportional to actual IV each cycle). An adjustment closes ONE side's
short+hedge pair and replaces it with a new short (chosen by searching
outward from ATM for the strike whose OWN live premium matches the
threatened side's current premium) plus a matching new hedge — same
strike/chain-lookup discipline (round_to_nearest_strike + real listed
contract lookup) rule_strategy.py and otm_put_roll_strategy.py both use.
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
from strategies.custom.smart_condor_schema import get_setting
from utils.logger import get_logger
from utils.option_utils import find_instrument_token, find_weekly_expiries, round_to_nearest_strike

log = get_logger(__name__)

_PRODUCT = "NRML"
_MAX_MATCH_SEARCH_STEPS = 40


class SmartCondorStrategy:
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

    def resolve_weekly_expiry(self) -> str:
        """The expiry `dte_weeks_offset` weeks out (0=nearest, 1="next week's" — see find_weekly_expiries)."""
        expiries = self.broker.get_option_contracts(self.instrument_key)
        if not expiries:
            raise RuntimeError(f"{self.symbol}: no option expiries returned.")
        now = self.broker.get_current_time()
        weekly = find_weekly_expiries(expiries, reference_date=now.date() if now else None)
        offset = get_setting(self.rules, "dte_weeks_offset")
        if offset >= len(weekly):
            raise RuntimeError(f"{self.symbol}: only {len(weekly)} weekly expiry(ies) listed, need index {offset} — refusing to guess a further one.")
        return weekly[offset]

    def get_spot(self) -> float:
        spot = self.broker.get_ltp(self.instrument_key)
        if spot is None:
            raise RuntimeError(f"{self.symbol}: could not fetch spot LTP.")
        return spot

    def get_chain(self, expiry: str) -> list:
        chain = self.broker.get_option_chain(self.instrument_key, expiry)
        if not chain:
            raise RuntimeError(f"{self.symbol}: empty option chain for expiry {expiry}.")
        return chain

    def compute_initial_strikes(self, spot: float, chain: list) -> dict:
        """ATM straddle premium (rounded to premium_round_points) becomes the offset for both short strikes; hedges sit hedge_points beyond each short."""
        atm_strike = round_to_nearest_strike(spot, self.strike_step)
        ce_token = find_instrument_token(chain, atm_strike, "CE")
        pe_token = find_instrument_token(chain, atm_strike, "PE")
        if not ce_token or not pe_token:
            raise ComplianceError(f"{self.symbol}: no listed ATM {atm_strike} CE/PE contract to price the entry straddle from.")
        ce_premium = self.broker.get_ltp(ce_token)
        pe_premium = self.broker.get_ltp(pe_token)
        if not ce_premium or not pe_premium:
            raise RuntimeError(f"{self.symbol}: could not fetch live ATM {atm_strike} CE/PE premiums — refusing to guess.")

        round_points = get_setting(self.rules, "premium_round_points")
        straddle_premium = ce_premium + pe_premium
        offset = round(straddle_premium / round_points) * round_points
        hedge_points = get_setting(self.rules, "hedge_points")

        short_ce = round_to_nearest_strike(atm_strike + offset, self.strike_step)
        short_pe = round_to_nearest_strike(atm_strike - offset, self.strike_step)
        return {
            "atm_strike": atm_strike,
            "straddle_premium": straddle_premium,
            "short_ce": short_ce,
            "short_pe": short_pe,
            "long_ce": round_to_nearest_strike(short_ce + hedge_points, self.strike_step),
            "long_pe": round_to_nearest_strike(short_pe - hedge_points, self.strike_step),
        }

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
            option_type=option_type, strike=strike, quantity=quantity, status="PENDING", note=f"smart_condor action={action}",
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

    def sell_short(self, chain: list, expiry: str, option_type: str, strike: float) -> dict:
        token = self._resolve_token(chain, strike, option_type)
        quantity = self._quantity()
        fill = self._place("SELL", token, quantity, strike, option_type, f"SCONDOR_{self.symbol[:5]}_SHORT")
        return {"instrument_token": token, "strike": strike, "expiry": expiry, "option_type": option_type, "quantity": quantity, "entry_price": fill["price"], "order_id": fill["order_id"], "transaction_type": "SELL"}

    def buy_hedge(self, chain: list, expiry: str, option_type: str, strike: float) -> dict:
        token = self._resolve_token(chain, strike, option_type)
        quantity = self._quantity()
        fill = self._place("BUY", token, quantity, strike, option_type, f"SCONDOR_{self.symbol[:5]}_HEDGE")
        return {"instrument_token": token, "strike": strike, "expiry": expiry, "option_type": option_type, "quantity": quantity, "entry_price": fill["price"], "order_id": fill["order_id"], "transaction_type": "BUY"}

    def enter(self, expiry: str) -> list[dict]:
        """First entry of a cycle: all 4 legs (short CE, short PE, long CE hedge, long PE hedge)."""
        spot = self.get_spot()
        chain = self.get_chain(expiry)
        strikes = self.compute_initial_strikes(spot, chain)

        short_ce = self.sell_short(chain, expiry, "CE", strikes["short_ce"])
        short_pe = self.sell_short(chain, expiry, "PE", strikes["short_pe"])
        long_ce = self.buy_hedge(chain, expiry, "CE", strikes["long_ce"])
        long_pe = self.buy_hedge(chain, expiry, "PE", strikes["long_pe"])

        short_ce["role"], short_ce["side"] = "SHORT", "CALL"
        short_pe["role"], short_pe["side"] = "SHORT", "PUT"
        long_ce["role"], long_ce["side"] = "HEDGE", "CALL"
        long_pe["role"], long_pe["side"] = "HEDGE", "PUT"
        return [short_ce, short_pe, long_ce, long_pe]

    def close_leg(self, instrument_token: str, quantity: int, strike: float, option_type: str, original_transaction_type: str) -> dict:
        """Reverses whichever side originally opened this leg — BUY to close a short, SELL to close a long hedge."""
        action = "BUY" if original_transaction_type == "SELL" else "SELL"
        fill = self._place(action, instrument_token, quantity, strike, option_type, f"SCONDOR_{self.symbol[:5]}_CLOSE", is_close=True)
        return {"exit_price": fill["price"], "order_id": fill["order_id"]}

    def resolve_matching_strike(self, chain: list, option_type: str, direction_sign: int, atm_strike: float, target_premium: float) -> float:
        """
        Search outward from ATM in `direction_sign`'s direction (+1 for
        CALL/upward, -1 for PUT/downward) for the strike whose OWN live
        premium is closest to target_premium — since premium decreases
        monotonically moving away from ATM, this finds where the two
        straddle either side of the target and returns whichever is
        closer, same "closest real match, never guess" discipline as
        rule_strategy.py's PREMIUM_BAND leg resolution.
        """
        prev_strike: float | None = None
        prev_premium: float | None = None
        for step in range(0, _MAX_MATCH_SEARCH_STEPS + 1):
            candidate = round_to_nearest_strike(atm_strike + direction_sign * step * self.strike_step, self.strike_step)
            token = find_instrument_token(chain, candidate, option_type)
            if not token:
                continue
            premium = self.broker.get_ltp(token)
            if premium is None:
                continue
            if prev_premium is not None and premium <= target_premium:
                if abs(prev_premium - target_premium) <= abs(premium - target_premium):
                    return prev_strike
                return candidate
            prev_strike, prev_premium = candidate, premium
        raise ComplianceError(f"{self.symbol}: no {option_type} strike within {_MAX_MATCH_SEARCH_STEPS} strikes of ATM {atm_strike} has a live premium near ₹{target_premium:.2f} — refusing to guess.")

    def adjust(self, expiry: str, safe_option_type: str, threatened_short_premium: float) -> tuple[dict, dict]:
        """Re-center the safe side: sell a new short whose premium matches the threatened side's current premium, plus a matching new hedge. Returns (new_short, new_hedge)."""
        spot = self.get_spot()
        chain = self.get_chain(expiry)
        atm_strike = round_to_nearest_strike(spot, self.strike_step)
        direction_sign = 1 if safe_option_type == "CE" else -1
        hedge_points = get_setting(self.rules, "hedge_points")

        new_short_strike = self.resolve_matching_strike(chain, safe_option_type, direction_sign, atm_strike, threatened_short_premium)
        new_hedge_strike = round_to_nearest_strike(new_short_strike + direction_sign * hedge_points, self.strike_step)

        new_short = self.sell_short(chain, expiry, safe_option_type, new_short_strike)
        new_hedge = self.buy_hedge(chain, expiry, safe_option_type, new_hedge_strike)
        side = "CALL" if safe_option_type == "CE" else "PUT"
        new_short["role"], new_short["side"] = "SHORT", side
        new_hedge["role"], new_hedge["side"] = "HEDGE", side
        return new_short, new_hedge
