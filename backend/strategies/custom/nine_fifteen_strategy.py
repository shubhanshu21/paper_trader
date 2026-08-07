"""
strategies/custom/nine_fifteen_strategy.py — executor for the "9:15
Opening Range Breakout" stock-options-buying strategy (see
nine_fifteen_schema.py for the rules contract, and
api/nine_fifteen_engine.py for the scheduler that owns entry/exit TIMING
and open-position state — this class only knows how to scan today's top
F&O movers and place/report a BUY order for one of them; never when to
call either).

Unlike every other engine in this app (Zero to Hero included), this
class is NOT constructed against one fixed symbol — the whole point of
the strategy is that WHICH stock gets traded is decided fresh every
morning from a live market scan, so `symbol` is a per-call argument to
resolve_leg()/enter(), never an __init__ parameter. This is also (like
Zero to Hero) a BUY-to-open strategy — see that module's docstring for
why that's safe on this codebase's brokers.
"""
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
from utils.instrument_cache import InstrumentCache
from utils.logger import get_logger
from utils.option_utils import (
    find_instrument_token,
    find_nearest_expiry_by_type,
    round_to_nearest_strike,
)

log = get_logger(__name__)


class NineFifteenStrategy:
    """Args mirror IntradaySupertrendStrategy's, minus `symbol` (see this module's docstring for why)."""

    def __init__(
        self,
        broker: BaseBroker,
        audit: AuditTrail,
        kill_switch: KillSwitch,
        rate_limiter: OrderRateLimiter,
        rules: dict,
        product: str = "MIS",
        user_id: int | None = None,
    ) -> None:
        self.broker = broker
        self.audit = audit
        self.kill_switch = kill_switch
        self.rate_limiter = rate_limiter
        self.rules = rules
        self.product = product.upper()
        self.user_id = user_id

    def scan_top_movers(self) -> dict | None:
        """
        Rank every F&O-eligible stock by live % change from its previous
        close and return {"top_gainer": {...}, "top_loser": {...}} — the
        real-broker-data equivalent of the video's "NSE website Top
        Gainers/Losers, filtered by F&O" step, via one (or a couple, batch
        size permitting) real API call rather than one-per-stock. Each
        entry: {"symbol", "instrument_key", "pct_change", "today_open",
        "ltp"}. Returns None if no F&O stock could be resolved/priced at
        all — never a partial/guessed ranking.

        `today_open` is the value entry-trigger checks compare later
        ticks' LTP against — captured here from the broker's own OHLC
        quote (the real session open), not sampled at whatever moment
        this scan happens to run.
        """
        universe = InstrumentCache().list_tradable_symbols().get("stocks", [])
        if not universe:
            return None

        key_by_symbol: dict[str, str] = {}
        for symbol in universe:
            try:
                key_by_symbol[symbol] = self.broker.resolve_instrument_key(symbol)
            except Exception:
                continue
        if not key_by_symbol:
            return None

        ohlc = self.broker.get_ohlc_batch(list(key_by_symbol.values()))

        candidates = []
        for symbol, key in key_by_symbol.items():
            row = ohlc.get(key)
            if not row or not row.get("prev_close") or row.get("ltp") is None:
                continue
            pct_change = (row["ltp"] - row["prev_close"]) / row["prev_close"] * 100
            candidates.append({
                "symbol": symbol, "instrument_key": key, "pct_change": pct_change,
                "today_open": row.get("today_open"), "ltp": row["ltp"],
            })
        if not candidates:
            return None

        candidates.sort(key=lambda c: c["pct_change"])
        return {"top_loser": candidates[0], "top_gainer": candidates[-1]}

    def resolve_leg(self, symbol: str, instrument_key: str, option_type: str, quantity: int) -> dict:
        """ATM `option_type` contract on the nearest listed expiry for `symbol` — same pre-trade price-band/freeze-quantity compliance checks as every other order path in this app."""
        spot = self.broker.get_ltp(instrument_key)
        if spot is None or spot <= 0:
            raise RuntimeError(f"Invalid/missing LTP for '{symbol}'.")

        strike_step = self.broker.get_strike_step(symbol)
        if strike_step is None:
            raise RuntimeError(f"Could not resolve a real strike step for '{symbol}' — refusing to guess.")

        expiries = self.broker.get_option_contracts(instrument_key)
        if not expiries:
            raise RuntimeError(f"No option expiries returned for '{symbol}'.")
        now = self.broker.get_current_time()
        # "WEEKLY" here means "nearest expiry of any kind" per
        # find_nearest_expiry_by_type's own contract — most F&O stocks
        # only list monthly contracts, so this naturally converges to
        # "nearest monthly" for them and "nearest weekly" for the handful
        # that have weeklies, with no per-symbol special-casing needed.
        expiry = find_nearest_expiry_by_type(expiries, "WEEKLY", reference_date=now.date() if now else None)
        if not expiry:
            raise RuntimeError(f"Could not determine nearest expiry for '{symbol}'.")

        chain = self.broker.get_option_chain(instrument_key, expiry)
        if not chain:
            raise RuntimeError(f"Empty option chain for {symbol} expiry {expiry}.")

        strike = round_to_nearest_strike(spot, strike_step)
        token = find_instrument_token(chain, strike, option_type)
        if not token:
            raise ComplianceError(f"No listed contract for {symbol} {strike} {option_type} expiry {expiry}.")

        try:
            validate_price_band(strike, spot)
        except ValueError as exc:
            raise ComplianceError(str(exc)) from exc
        validate_order_quantity(symbol, quantity)

        return {"instrument_token": token, "strike": strike, "option_type": option_type, "expiry": expiry, "quantity": quantity}

    def enter(self, symbol: str, instrument_key: str, option_type: str, quantity: int) -> dict:
        """BUY `quantity` of the ATM `option_type` leg on `symbol` — going LONG premium (see this module's docstring). Raises on a hard failure; the scheduler decides how to log/notify/retry."""
        leg = self.resolve_leg(symbol, instrument_key, option_type, quantity)
        assert_kill_switch_not_active(self.kill_switch)
        self.rate_limiter.acquire()
        self.audit.record(
            event_type="ORDER_INITIATED", symbol=symbol, instrument_token=leg["instrument_token"],
            option_type=option_type, strike=leg["strike"], quantity=leg["quantity"], status="PENDING",
            note="nine_fifteen_entry",
        )
        order_id = self.broker.place_buy_order(
            instrument_token=leg["instrument_token"], quantity=leg["quantity"], product=self.product,
            order_type="MARKET", tag=f"915ORB_{option_type}_{symbol[:6]}"[:20], user_id=self.user_id,
        )
        if not self.broker.dry_run and not order_id:
            self.audit.record(
                event_type="ORDER_FAILED", symbol=symbol, instrument_token=leg["instrument_token"],
                option_type=option_type, strike=leg["strike"], quantity=leg["quantity"], status="FAILED",
            )
            raise RuntimeError(f"{symbol}: BUY {option_type} order failed to place.")

        fill_price = self.broker.get_fill_price(order_id) if order_id else None
        entry_price = fill_price if fill_price is not None else self.broker.get_ltp(leg["instrument_token"])
        self.audit.record(
            event_type="ORDER_PLACED", symbol=symbol, instrument_token=leg["instrument_token"],
            option_type=option_type, strike=leg["strike"], quantity=leg["quantity"],
            order_id=order_id or "DRY_RUN", status="PLACED",
        )
        leg["order_id"] = order_id
        leg["entry_price"] = entry_price
        leg["transaction_type"] = "BUY"
        return leg
