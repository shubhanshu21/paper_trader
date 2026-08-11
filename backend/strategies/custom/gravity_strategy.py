"""
strategies/custom/gravity_strategy.py — executor for the "Gravity"
Camarilla fakeout-reversal credit-spread strategy (see gravity_schema.py
for the rules contract and why this needed its own engine).

One 2-leg credit spread open at a time (SOLD near-the-fakeout-extreme
option + a further-OTM HEDGE). Signal evaluation reads real daily candles
(never a raw LTP tick) and is stateless by design — it re-derives "did
price breach then close back inside R3/S3" fresh from recent history on
every check, rather than persisting a "currently in a breach" flag, so a
missed tick or a restart can never leave it in a stuck/ambiguous state.
"""
import math
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
from strategies.custom.gravity_schema import get_setting
from utils.logger import get_logger
from utils.option_utils import (
    find_expiry_by_type_offset,
    find_instrument_token,
    find_nearest_expiry_by_type,
)
from utils.technical_indicators import camarilla_pivot_points

log = get_logger(__name__)

_PRODUCT = "NRML"


def _month_key(timestamp: str) -> str:
    return timestamp[:7]  # 'YYYY-MM' — Upstox candle timestamps are ISO 8601


class GravityStrategy:
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
        reference_date = now.date() if now else None
        offset = get_setting(self.rules, "expiry_offset")
        if offset:
            expiry = find_expiry_by_type_offset(expiries, "MONTHLY", offset, reference_date=reference_date)
        else:
            expiry = find_nearest_expiry_by_type(expiries, "MONTHLY", reference_date=reference_date)
        if not expiry:
            raise RuntimeError(f"{self.symbol}: could not resolve a monthly expiry.")
        return expiry

    def get_daily_candles(self) -> list[dict]:
        """Most-recent-first, per BaseBroker.get_historical_candles's contract."""
        candles = self.broker.get_historical_candles(self.instrument_key, "days", 1, date.today().isoformat())
        if not candles:
            raise RuntimeError(f"{self.symbol}: no daily candles available.")
        return candles

    def _prev_month_ohlc(self, candles: list[dict]) -> dict:
        """
        The PREVIOUS calendar month's high/low/close, scanned off the
        most-recent-first daily candle list — Camarilla's whole premise is
        pivots derived from the prior period's range, monthly here (see
        gravity_schema.py). Skips whatever prefix of `candles` belongs to
        the CURRENT month, then collects every candle in the next
        (previous) month it finds.
        """
        current_month = _month_key(candles[0]["timestamp"])
        prev_month_candles = []
        prev_month_key = None
        for c in candles:
            key = _month_key(c["timestamp"])
            if key == current_month:
                continue
            if prev_month_key is None:
                prev_month_key = key
            if key != prev_month_key:
                break
            prev_month_candles.append(c)
        if not prev_month_candles:
            raise RuntimeError(f"{self.symbol}: not enough daily history to derive last month's OHLC.")
        return {
            "high": max(c["high"] for c in prev_month_candles),
            "low": min(c["low"] for c in prev_month_candles),
            "close": prev_month_candles[0]["close"],  # most-recent-first -> index 0 is the month's LAST trading day
        }

    def evaluate_signal(self) -> dict | None:
        """
        None (no signal), or {"signal": "BULLISH" | "BEARISH", "extreme":
        float, "r3": float, "s3": float}. BULLISH = yesterday's close was
        below S3 and today's closed back above it (a downside fakeout
        confirmed) -> sell a PUT credit spread. BEARISH is the mirror at
        R3 -> sell a CALL credit spread.
        """
        candles = self.get_daily_candles()
        if len(candles) < 3:
            return None
        levels = camarilla_pivot_points(*[self._prev_month_ohlc(candles)[k] for k in ("high", "low", "close")])
        r3, s3 = levels["r3"], levels["s3"]

        chron = list(reversed(candles))  # oldest-first, for the lookback/yesterday-vs-today reads below
        today_close = chron[-1]["close"]
        yesterday_close = chron[-2]["close"]
        lookback_days = get_setting(self.rules, "extreme_lookback_days")
        window = chron[-lookback_days:]

        if yesterday_close < s3 and today_close > s3:
            return {"signal": "BULLISH", "extreme": min(c["low"] for c in window), "r3": r3, "s3": s3}
        if yesterday_close > r3 and today_close < r3:
            return {"signal": "BEARISH", "extreme": max(c["high"] for c in window), "r3": r3, "s3": s3}
        return None

    def is_blacked_out(self, today: date | None = None) -> bool:
        today = today or date.today()
        for window in get_setting(self.rules, "blackout_dates") or []:
            if date.fromisoformat(window["start"]) <= today <= date.fromisoformat(window["end"]):
                return True
        return False

    # ------------------------------------------------------------------

    def compute_spread_strikes(self, signal: str, extreme: float) -> tuple[float, float]:
        """(sold_strike, hedge_strike). Sold strike is rounded AT-OR-BEYOND the extreme (floor for puts, ceil for calls) — "at or beyond the fakeout's own extreme" per the strategy's own rule, deliberately never rounding to a strike that's LESS OTM than the extreme itself."""
        hedge_away = get_setting(self.rules, "hedge_strikes_away")
        if signal == "BULLISH":
            sold_strike = math.floor(extreme / self.strike_step) * self.strike_step
            hedge_strike = sold_strike - hedge_away * self.strike_step
        else:
            sold_strike = math.ceil(extreme / self.strike_step) * self.strike_step
            hedge_strike = sold_strike + hedge_away * self.strike_step
        return sold_strike, hedge_strike

    def get_chain(self, expiry: str) -> list:
        chain = self.broker.get_option_chain(self.instrument_key, expiry)
        if not chain:
            raise RuntimeError(f"{self.symbol}: empty option chain for expiry {expiry}.")
        return chain

    def preview_spread(self, expiry: str, signal: str, extreme: float) -> dict:
        """
        Live-price the spread WITHOUT placing any orders — lets the engine
        apply the min_roi_pct filter (gravity_schema.py) before deciding
        whether to actually enter. Uses current LTPs as an estimate; the
        real entry (enter(), below) re-resolves and prices at ACTUAL fill
        time, so this is a pre-flight check, not a guaranteed quote.
        """
        option_type = "PE" if signal == "BULLISH" else "CE"
        sold_strike, hedge_strike = self.compute_spread_strikes(signal, extreme)
        chain = self.get_chain(expiry)
        sold_token = self._resolve_token(chain, sold_strike, option_type)
        hedge_token = self._resolve_token(chain, hedge_strike, option_type)
        sold_premium = self.broker.get_ltp(sold_token)
        hedge_premium = self.broker.get_ltp(hedge_token)
        if sold_premium is None or hedge_premium is None:
            raise RuntimeError(f"{self.symbol}: could not fetch live premiums for {sold_strike}/{hedge_strike} {option_type} — refusing to guess.")
        quantity = self.rules.get("lots", 1) * self.real_lot_size
        net_credit = (sold_premium - hedge_premium) * quantity
        max_loss = abs(sold_strike - hedge_strike) * quantity - net_credit
        return {"option_type": option_type, "sold_strike": sold_strike, "hedge_strike": hedge_strike, "net_credit": net_credit, "max_loss": max_loss}

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
            option_type=option_type, strike=strike, quantity=quantity, status="PENDING", note=f"gravity action={action}",
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

    def enter(self, expiry: str, signal: str, extreme: float) -> tuple[dict, dict]:
        """Returns (sold_leg, hedge_leg)."""
        option_type = "PE" if signal == "BULLISH" else "CE"
        sold_strike, hedge_strike = self.compute_spread_strikes(signal, extreme)
        quantity = self.rules.get("lots", 1) * self.real_lot_size
        validate_order_quantity(self.symbol, quantity)

        chain = self.get_chain(expiry)
        sold_token = self._resolve_token(chain, sold_strike, option_type)
        hedge_token = self._resolve_token(chain, hedge_strike, option_type)

        sold_fill = self._place("SELL", sold_token, quantity, sold_strike, option_type, f"GRAVITY_{self.symbol[:4]}_SOLD")
        hedge_fill = self._place("BUY", hedge_token, quantity, hedge_strike, option_type, f"GRAVITY_{self.symbol[:4]}_HEDGE")

        sold_leg = {"instrument_token": sold_token, "strike": sold_strike, "expiry": expiry, "option_type": option_type, "quantity": quantity, "entry_price": sold_fill["price"], "order_id": sold_fill["order_id"], "transaction_type": "SELL", "role": "SOLD"}
        hedge_leg = {"instrument_token": hedge_token, "strike": hedge_strike, "expiry": expiry, "option_type": option_type, "quantity": quantity, "entry_price": hedge_fill["price"], "order_id": hedge_fill["order_id"], "transaction_type": "BUY", "role": "HEDGE"}
        return sold_leg, hedge_leg

    def close_leg(self, instrument_token: str, quantity: int, strike: float, option_type: str, original_transaction_type: str) -> dict:
        action = "BUY" if original_transaction_type == "SELL" else "SELL"
        fill = self._place(action, instrument_token, quantity, strike, option_type, f"GRAVITY_{self.symbol[:4]}_CLOSE", is_close=True)
        return {"exit_price": fill["price"], "order_id": fill["order_id"]}

    @staticmethod
    def net_credit_and_max_loss(sold_leg: dict, hedge_leg: dict) -> tuple[float, float]:
        """(total net credit collected, total max loss) for this 2-leg spread, in rupees, at entry prices."""
        per_share_credit = (sold_leg["entry_price"] or 0) - (hedge_leg["entry_price"] or 0)
        quantity = sold_leg["quantity"]
        net_credit = per_share_credit * quantity
        spread_width = abs(sold_leg["strike"] - hedge_leg["strike"])
        max_loss = spread_width * quantity - net_credit
        return net_credit, max_loss
