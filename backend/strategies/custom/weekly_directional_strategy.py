"""
strategies/custom/weekly_directional_strategy.py — executor for the
"Weekly Directional" asymmetric Reverse-Iron-Fly-with-tail-hedges
strategy (see weekly_directional_schema.py for the rules contract and
why this needed its own engine).

5 legs open at once: BUY ATM CE, BUY ATM PE (the straddle), SELL OTM PE,
SELL OTM CE (asymmetric — one side 2x lots, funding the straddle and
skewing the payoff toward that side's direction), BUY a deep-OTM
delta-targeted tail hedge (2x lots, on the same side as the 2x short) to
cap tail risk on a sharp move against the position.
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
from strategies.custom.weekly_directional_schema import get_setting
from utils import black76
from utils.instrument_cache import InstrumentCache
from utils.logger import get_logger
from utils.option_utils import (
    find_expiry_by_type_offset,
    find_instrument_token,
    find_nearest_expiry_by_type,
    round_to_nearest_strike,
)
from utils.technical_indicators import ema

log = get_logger(__name__)

_PRODUCT = "NRML"
_MAX_HEDGE_SEARCH_STEPS = 40


class WeeklyDirectionalStrategy:
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
        reference_date = now.date() if now else None
        offset = get_setting(self.rules, "expiry_offset")
        if offset:
            expiry = find_expiry_by_type_offset(expiries, "WEEKLY", offset, reference_date=reference_date)
        else:
            expiry = find_nearest_expiry_by_type(expiries, "WEEKLY", reference_date=reference_date)
        if not expiry:
            raise RuntimeError(f"{self.symbol}: could not resolve a weekly expiry.")
        return expiry

    def get_daily_candles(self) -> list[dict]:
        """Most-recent-first, per BaseBroker.get_historical_candles's contract."""
        candles = self.broker.get_historical_candles(self.instrument_key, "days", 1, date.today().isoformat())
        if not candles:
            raise RuntimeError(f"{self.symbol}: no daily candles available.")
        return candles

    def determine_direction(self, candles: list[dict] | None = None) -> str | None:
        """
        "BULLISH" (EMA(ema_fast) > EMA(ema_slow)), "BEARISH" (<), or None
        (exactly equal, or not enough history yet — no clear bias, skip
        this week's entry). See module docstring for why this is a
        current-STATE read, not a literal crossing-event check.
        """
        candles = candles if candles is not None else self.get_daily_candles()
        chron = list(reversed(candles))  # oldest-first, what ema() expects
        closes = [c["close"] for c in chron]
        fast_period, slow_period = get_setting(self.rules, "ema_fast"), get_setting(self.rules, "ema_slow")
        fast_vals, slow_vals = ema(closes, fast_period), ema(closes, slow_period)
        if not fast_vals or not slow_vals or fast_vals[-1] is None or slow_vals[-1] is None:
            return None
        fast, slow = fast_vals[-1], slow_vals[-1]
        if fast > slow:
            return "BULLISH"
        if fast < slow:
            return "BEARISH"
        return None

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
        """Black-76 needs a forward/futures price, not spot — same resolve_nearest_future_key() -> get_ltp() pattern api/live_greeks.py / delta_neutral_strategy.py use."""
        resolved = self._instrument_cache.resolve_nearest_future_key(self.symbol)
        price = self.broker.get_ltp(resolved[0]) if resolved else None
        if price is None:
            raise RuntimeError(f"{self.symbol}: could not resolve a live futures price for Black-76 delta.")
        return price

    def time_to_expiry_years(self, expiry: str) -> float:
        days = max((date.fromisoformat(expiry) - date.today()).days, 1)
        return days / 365.0

    # ------------------------------------------------------------------

    def compute_leg_plan(self, direction: str, atm_strike: float) -> dict:
        """
        Pure strike/sizing plan (no live pricing/order placement) — the
        heavier-sold (2x) side and the tail-hedge side always match:
        BEARISH sells 2x calls (profits most from a fall, decaying CE
        premium) + hedges the call side; BULLISH is the mirror on puts.
        """
        otm_points = get_setting(self.rules, "short_otm_points")
        short_put_strike = round_to_nearest_strike(atm_strike - otm_points, self.strike_step)
        short_call_strike = round_to_nearest_strike(atm_strike + otm_points, self.strike_step)
        heavier_side = "CE" if direction == "BEARISH" else "PE"
        return {
            "atm_strike": atm_strike,
            "short_put_strike": short_put_strike,
            "short_call_strike": short_call_strike,
            "put_lots_multiplier": 2 if heavier_side == "PE" else 1,
            "call_lots_multiplier": 2 if heavier_side == "CE" else 1,
            "tail_hedge_option_type": heavier_side,
        }

    def find_tail_hedge_strike(self, chain: list, option_type: str, atm_strike: float, expiry: str, futures_price: float) -> float:
        """
        Nearest strike (searching outward from ATM in option_type's OTM
        direction) whose live |delta| is closest to tail_hedge_target_delta
        — delta falls off monotonically moving away from ATM, so the first
        strike at/below target and its immediate predecessor bracket the
        answer; same pattern as delta_neutral_strategy.py's find_strike_by_delta.
        """
        target_delta = get_setting(self.rules, "tail_hedge_target_delta")
        sign = 1 if option_type == "CE" else -1
        years_to_expiry = self.time_to_expiry_years(expiry)
        prev_strike: float | None = None
        prev_delta: float | None = None
        for step in range(1, _MAX_HEDGE_SEARCH_STEPS + 1):
            candidate = round_to_nearest_strike(atm_strike + sign * step * self.strike_step, self.strike_step)
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
        raise ComplianceError(f"{self.symbol}: no {option_type} strike within {_MAX_HEDGE_SEARCH_STEPS} strikes of ATM {atm_strike} has a live delta near {target_delta} — refusing to guess.")

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
            option_type=option_type, strike=strike, quantity=quantity, status="PENDING", note=f"weekly_directional action={action}",
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

    def enter(self, expiry: str, direction: str) -> list[dict]:
        """All 5 legs of one cycle: BUY ATM CE, BUY ATM PE, SELL OTM PE, SELL OTM CE, BUY tail hedge."""
        atm_strike = self.get_atm_strike()
        plan = self.compute_leg_plan(direction, atm_strike)
        chain = self.get_chain(expiry)
        futures_price = self.get_futures_price()

        base_qty = self._quantity(1)
        put_qty = self._quantity(plan["put_lots_multiplier"])
        call_qty = self._quantity(plan["call_lots_multiplier"])
        hedge_qty = self._quantity(2)

        atm_ce_token = self._resolve_token(chain, atm_strike, "CE")
        atm_pe_token = self._resolve_token(chain, atm_strike, "PE")
        short_pe_token = self._resolve_token(chain, plan["short_put_strike"], "PE")
        short_ce_token = self._resolve_token(chain, plan["short_call_strike"], "CE")
        hedge_strike = self.find_tail_hedge_strike(chain, plan["tail_hedge_option_type"], atm_strike, expiry, futures_price)
        hedge_token = self._resolve_token(chain, hedge_strike, plan["tail_hedge_option_type"])

        atm_ce_fill = self._place("BUY", atm_ce_token, base_qty, atm_strike, "CE", f"WDIR_{self.symbol[:4]}_ATMCE")
        atm_pe_fill = self._place("BUY", atm_pe_token, base_qty, atm_strike, "PE", f"WDIR_{self.symbol[:4]}_ATMPE")
        short_pe_fill = self._place("SELL", short_pe_token, put_qty, plan["short_put_strike"], "PE", f"WDIR_{self.symbol[:4]}_SPE")
        short_ce_fill = self._place("SELL", short_ce_token, call_qty, plan["short_call_strike"], "CE", f"WDIR_{self.symbol[:4]}_SCE")
        hedge_fill = self._place("BUY", hedge_token, hedge_qty, hedge_strike, plan["tail_hedge_option_type"], f"WDIR_{self.symbol[:4]}_TAIL")

        def _leg(fill, token, strike, option_type, qty, transaction_type, role):
            return {
                "instrument_token": token, "strike": strike, "expiry": expiry, "option_type": option_type,
                "quantity": qty, "entry_price": fill["price"], "order_id": fill["order_id"],
                "transaction_type": transaction_type, "role": role,
            }

        return [
            _leg(atm_ce_fill, atm_ce_token, atm_strike, "CE", base_qty, "BUY", "STRADDLE"),
            _leg(atm_pe_fill, atm_pe_token, atm_strike, "PE", base_qty, "BUY", "STRADDLE"),
            _leg(short_pe_fill, short_pe_token, plan["short_put_strike"], "PE", put_qty, "SELL", "SHORT"),
            _leg(short_ce_fill, short_ce_token, plan["short_call_strike"], "CE", call_qty, "SELL", "SHORT"),
            _leg(hedge_fill, hedge_token, hedge_strike, plan["tail_hedge_option_type"], hedge_qty, "BUY", "TAIL_HEDGE"),
        ]

    def close_leg(self, instrument_token: str, quantity: int, strike: float, option_type: str, original_transaction_type: str) -> dict:
        action = "BUY" if original_transaction_type == "SELL" else "SELL"
        fill = self._place(action, instrument_token, quantity, strike, option_type, f"WDIR_{self.symbol[:4]}_CLOSE", is_close=True)
        return {"exit_price": fill["price"], "order_id": fill["order_id"]}
