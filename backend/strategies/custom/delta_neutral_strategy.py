"""
strategies/custom/delta_neutral_strategy.py — executor for the Monthly
Delta-Neutral Strangle with Dynamic Adjustment strategy (see
delta_neutral_schema.py for the rules contract and why this needed its
own engine).

Up to 4 legs open at once (short CE, short PE, hedge CE, hedge PE).
Delta is always solved LIVE from the real traded premium (Black-76,
utils/black76.py) — never assumed or approximated — same discipline
api/live_greeks.py already uses for the strategy detail page's Greeks
panel, reused directly here (compute_greeks_from_market_price).
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
from strategies.custom.delta_neutral_schema import get_setting
from utils import black76
from utils.instrument_cache import InstrumentCache
from utils.logger import get_logger
from utils.option_utils import (
    find_instrument_token,
    find_nearest_expiry_by_type,
    find_weekly_expiries,
    round_to_nearest_strike,
)

log = get_logger(__name__)

_PRODUCT = "NRML"  # monthly/swing, never MIS
_MAX_SEARCH_STEPS = 30


class DeltaNeutralStrategy:
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

    def resolve_expiry(self) -> str:
        expiries = self.broker.get_option_contracts(self.instrument_key)
        if not expiries:
            raise RuntimeError(f"{self.symbol}: no option expiries returned.")
        now = self.broker.get_current_time()
        expiry = find_nearest_expiry_by_type(expiries, "MONTHLY", reference_date=now.date() if now else None)
        if not expiry:
            raise RuntimeError(f"{self.symbol}: could not resolve the nearest monthly expiry.")
        return expiry

    def resolve_third_weekly_expiry(self, monthly_expiry: str) -> str:
        """The 3rd weekly expiry falling in `monthly_expiry`'s own calendar month — the reference strategy's deliberate "not the 4th/last week" time-exit anchor."""
        expiries = self.broker.get_option_contracts(self.instrument_key)
        if not expiries:
            raise RuntimeError(f"{self.symbol}: no option expiries returned.")
        target_month = monthly_expiry[:7]  # 'YYYY-MM'
        weeklies = [e for e in find_weekly_expiries(expiries) if e[:7] == target_month]
        if len(weeklies) < 3:
            raise RuntimeError(f"{self.symbol}: only {len(weeklies)} weekly expiry(ies) listed in {target_month} — can't resolve the 3rd.")
        return weeklies[2]

    def get_atm_strike(self) -> float:
        spot = self.broker.get_ltp(self.instrument_key)
        if spot is None:
            raise RuntimeError(f"{self.symbol}: could not fetch spot LTP.")
        return round_to_nearest_strike(spot, self.strike_step)

    def get_futures_price(self) -> float:
        """Black-76 needs a forward/futures price, not spot — same resolve_nearest_future_key() -> get_ltp() pattern api/live_greeks.py uses."""
        resolved = self._instrument_cache.resolve_nearest_future_key(self.symbol)
        price = self.broker.get_ltp(resolved[0]) if resolved else None
        if price is None:
            raise RuntimeError(f"{self.symbol}: could not resolve a live futures price for Black-76 delta.")
        return price

    def get_chain(self, expiry: str) -> list:
        chain = self.broker.get_option_chain(self.instrument_key, expiry)
        if not chain:
            raise RuntimeError(f"{self.symbol}: empty option chain for expiry {expiry}.")
        return chain

    def time_to_expiry_years(self, expiry: str) -> float:
        days = max((date.fromisoformat(expiry) - date.today()).days, 1)
        return days / 365.0

    def _premium_at(self, chain: list, strike: float, option_type: str) -> float | None:
        token = find_instrument_token(chain, strike, option_type)
        if not token:
            return None
        return self.broker.get_ltp(token)

    def live_delta(self, strike: float, option_type: str, premium: float, expiry: str, futures_price: float) -> float | None:
        """abs(delta) — direction is implied by option_type, callers only ever compare magnitudes."""
        g = black76.compute_greeks_from_market_price(
            F=futures_price, K=strike, T=self.time_to_expiry_years(expiry), r=black76.DEFAULT_RISK_FREE_RATE,
            market_price=premium, option_type=option_type,
        )
        return abs(g["delta"]) if g else None

    # ------------------------------------------------------------------

    def _grid_strike(self, atm_strike: float, sign: int, step: int) -> float:
        grid = get_setting(self.rules, "strike_grid")
        return round_to_nearest_strike(atm_strike + sign * step * grid, self.strike_step)

    def find_strike_by_delta(self, chain: list, option_type: str, atm_strike: float, expiry: str, futures_price: float, target_delta: float) -> float:
        """Strike (on the strike_grid) whose live |delta| is closest to target_delta — delta falls off monotonically moving away from ATM, so the first strike at/below target and its immediate predecessor bracket the answer; return whichever is closer, same pattern as smart_condor_strategy.py's resolve_matching_strike()."""
        sign = 1 if option_type == "CE" else -1
        prev_strike: float | None = None
        prev_delta: float | None = None
        for step in range(1, _MAX_SEARCH_STEPS + 1):
            candidate = self._grid_strike(atm_strike, sign, step)
            premium = self._premium_at(chain, candidate, option_type)
            if premium is None:
                continue
            delta = self.live_delta(candidate, option_type, premium, expiry, futures_price)
            if delta is None:
                continue
            if prev_delta is not None and delta <= target_delta:
                return prev_strike if abs(prev_delta - target_delta) <= abs(delta - target_delta) else candidate
            prev_strike, prev_delta = candidate, delta
        raise ComplianceError(f"{self.symbol}: no {option_type} strike within {_MAX_SEARCH_STEPS} grid steps of ATM {atm_strike} has a live delta near {target_delta} — refusing to guess.")

    def find_strike_by_premium(self, chain: list, option_type: str, atm_strike: float, target_premium: float) -> float:
        """Strike (on the strike_grid) whose live premium is closest to target_premium — used by the Stage 2 full reset, which re-sells by PREMIUM rather than delta."""
        sign = 1 if option_type == "CE" else -1
        prev_strike: float | None = None
        prev_premium: float | None = None
        for step in range(1, _MAX_SEARCH_STEPS + 1):
            candidate = self._grid_strike(atm_strike, sign, step)
            premium = self._premium_at(chain, candidate, option_type)
            if premium is None:
                continue
            if prev_premium is not None and premium <= target_premium:
                return prev_strike if abs(prev_premium - target_premium) <= abs(premium - target_premium) else candidate
            prev_strike, prev_premium = candidate, premium
        raise ComplianceError(f"{self.symbol}: no {option_type} strike within {_MAX_SEARCH_STEPS} grid steps of ATM {atm_strike} has a live premium near ₹{target_premium:.2f} — refusing to guess.")

    def find_hedge_strike(self, chain: list, atm_strike: float, option_type: str) -> float:
        """Cheapest real listed strike (searching outward from ATM on the real strike grid, not the wider strike_grid — a hedge doesn't need liquidity the same way the short legs do) within [hedge_premium_min, hedge_premium_max]."""
        band_min, band_max = get_setting(self.rules, "hedge_premium_min"), get_setting(self.rules, "hedge_premium_max")
        sign = 1 if option_type == "CE" else -1
        for step_n in range(1, _MAX_SEARCH_STEPS * 4 + 1):
            candidate = round_to_nearest_strike(atm_strike + sign * step_n * self.strike_step, self.strike_step)
            premium = self._premium_at(chain, candidate, option_type)
            if premium is not None and band_min <= premium <= band_max:
                return candidate
        raise ComplianceError(f"{self.symbol}: no {option_type} strike has a live premium between ₹{band_min} and ₹{band_max} — refusing to guess.")

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
            option_type=option_type, strike=strike, quantity=quantity, status="PENDING", note=f"delta_neutral action={action}",
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
        fill = self._place("SELL", token, quantity, strike, option_type, f"DNEUTRAL_{self.symbol[:4]}_SHORT")
        return {"instrument_token": token, "strike": strike, "expiry": expiry, "option_type": option_type, "quantity": quantity, "entry_price": fill["price"], "order_id": fill["order_id"], "transaction_type": "SELL", "role": "SHORT"}

    def buy_hedge(self, chain: list, expiry: str, option_type: str, strike: float) -> dict:
        token = self._resolve_token(chain, strike, option_type)
        quantity = self._quantity()
        fill = self._place("BUY", token, quantity, strike, option_type, f"DNEUTRAL_{self.symbol[:4]}_HEDGE")
        return {"instrument_token": token, "strike": strike, "expiry": expiry, "option_type": option_type, "quantity": quantity, "entry_price": fill["price"], "order_id": fill["order_id"], "transaction_type": "BUY", "role": "HEDGE"}

    def enter(self, expiry: str) -> list[dict]:
        """First entry of a cycle: short CE + short PE at target_delta, plus a cheap hedge each side."""
        atm = self.get_atm_strike()
        futures_price = self.get_futures_price()
        chain = self.get_chain(expiry)
        target_delta = get_setting(self.rules, "target_delta")

        short_ce_strike = self.find_strike_by_delta(chain, "CE", atm, expiry, futures_price, target_delta)
        short_pe_strike = self.find_strike_by_delta(chain, "PE", atm, expiry, futures_price, target_delta)
        hedge_ce_strike = self.find_hedge_strike(chain, atm, "CE")
        hedge_pe_strike = self.find_hedge_strike(chain, atm, "PE")

        legs = [
            self.sell_short(chain, expiry, "CE", short_ce_strike),
            self.sell_short(chain, expiry, "PE", short_pe_strike),
            self.buy_hedge(chain, expiry, "CE", hedge_ce_strike),
            self.buy_hedge(chain, expiry, "PE", hedge_pe_strike),
        ]
        return legs

    def close_leg(self, instrument_token: str, quantity: int, strike: float, option_type: str, original_transaction_type: str) -> dict:
        action = "BUY" if original_transaction_type == "SELL" else "SELL"
        fill = self._place(action, instrument_token, quantity, strike, option_type, f"DNEUTRAL_{self.symbol[:4]}_CLOSE", is_close=True)
        return {"exit_price": fill["price"], "order_id": fill["order_id"]}
