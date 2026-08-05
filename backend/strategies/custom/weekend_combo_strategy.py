"""
strategies/custom/weekend_combo_strategy.py — executor for the Nifty +
Sensex weekend-gap ratio-spread combo (see combo_schema.py for the rules
contract and why this is a genuinely separate engine, not a variant of
rule_schema.py/RuleBasedStrategy).

Resolves and places SIX legs across TWO DIFFERENT symbols (three each,
opposite directional bias) as ONE combined basket — reuses
rule_strategy.resolve_leg_strike()/option_utils.find_instrument_token()
for the actual strike math (identical OTM_POINTS convention, see
combo_schema.py's module docstring), it's only the "two symbols in one
basket" orchestration around them that's new.
"""
from broker.base_broker import BaseBroker
from compliance.sebi_rules import AuditTrail, ComplianceError, KillSwitch, OrderRateLimiter, validate_order_quantity, validate_price_band
from strategies.custom.combo_schema import legs_for
from strategies.custom.rule_strategy import resolve_leg_strike
from utils.logger import get_logger
from utils.option_utils import find_instrument_token, find_nearest_expiry_by_type

log = get_logger(__name__)

# NRML (not MIS) — this strategy explicitly HOLDS OVERNIGHT across the
# weekend + Monday, unlike the intraday Supertrend strategy's MIS.
_PRODUCT = "NRML"


class WeekendGapComboStrategy:
    def __init__(
        self,
        broker: BaseBroker,
        audit: AuditTrail,
        kill_switch: KillSwitch,
        rate_limiter: OrderRateLimiter,
        rules: dict,
        user_id: int | None = None,
    ) -> None:
        self.broker = broker
        self.audit = audit
        self.kill_switch = kill_switch
        self.rate_limiter = rate_limiter
        self.rules = rules
        self.user_id = user_id

    def _resolve_symbol_context(self, symbol: str) -> dict:
        instrument_key = self.broker.resolve_instrument_key(symbol)
        strike_step = self.broker.get_strike_step(symbol)
        if strike_step is None:
            raise RuntimeError(f"Could not resolve a real strike step for '{symbol}' — refusing to guess.")
        lot_size = self.broker.get_lot_size(symbol)
        if lot_size is None:
            raise RuntimeError(f"Could not resolve a real lot size for '{symbol}' — refusing to guess.")
        spot = self.broker.get_ltp(instrument_key)
        if spot is None or spot <= 0:
            raise RuntimeError(f"Invalid/missing LTP for '{symbol}'.")

        expiries = self.broker.get_option_contracts(instrument_key)
        if not expiries:
            raise RuntimeError(f"No option expiries returned for '{symbol}'.")
        now = self.broker.get_current_time()
        expiry = find_nearest_expiry_by_type(expiries, "WEEKLY", reference_date=now.date() if now else None)
        if not expiry:
            raise RuntimeError(f"Could not determine nearest weekly expiry for '{symbol}'.")
        chain = self.broker.get_option_chain(instrument_key, expiry)
        if not chain:
            raise RuntimeError(f"Empty option chain for {symbol} expiry {expiry}.")

        return {"instrument_key": instrument_key, "strike_step": strike_step, "lot_size": lot_size, "spot": spot, "expiry": expiry, "chain": chain}

    def resolve_legs(self, bias: str) -> list[dict]:
        """
        Resolve all six legs (three per symbol) for `bias` — see
        combo_schema.py's _SETUPS. Returns [{symbol, instrument_token,
        strike, option_type, action, quantity, expiry, spot}, ...],
        already price-band/freeze-quantity compliance-checked, before any
        order is placed.
        """
        setup = legs_for(bias)
        lots_cfg = self.rules.get("lots") or {}
        resolved: list[dict] = []
        for symbol, legs in setup.items():
            ctx = self._resolve_symbol_context(symbol)
            multiplier = lots_cfg.get(symbol, 1)
            for leg in legs:
                strike = resolve_leg_strike(
                    {"option_type": leg["option_type"], "strike_selection": {"mode": "OTM_POINTS", "value": leg["otm_points"]}},
                    ctx["spot"], ctx["strike_step"],
                )
                token = find_instrument_token(ctx["chain"], strike, leg["option_type"])
                if not token:
                    raise ComplianceError(f"No listed contract for {symbol} {strike} {leg['option_type']} expiry {ctx['expiry']}.")
                quantity = leg["lots"] * multiplier * ctx["lot_size"]

                try:
                    validate_price_band(strike, ctx["spot"])
                except ValueError as exc:
                    raise ComplianceError(str(exc)) from exc
                validate_order_quantity(symbol, quantity)

                resolved.append({
                    "symbol": symbol, "instrument_token": token, "strike": strike,
                    "option_type": leg["option_type"], "action": leg["action"],
                    "quantity": quantity, "expiry": ctx["expiry"], "spot": ctx["spot"],
                })
        return resolved

    def _place_leg(self, leg: dict, idx: int) -> str | None:
        self.rate_limiter.acquire()
        place = self.broker.place_sell_order if leg["action"] == "SELL" else self.broker.place_buy_order
        self.audit.record(
            event_type="ORDER_INITIATED", symbol=leg["symbol"], instrument_token=leg["instrument_token"],
            option_type=leg["option_type"], strike=leg["strike"], quantity=leg["quantity"], status="PENDING",
            note=f"weekend_combo_leg_{idx} action={leg['action']}",
        )
        order_id = place(
            instrument_token=leg["instrument_token"], quantity=leg["quantity"], product=_PRODUCT,
            order_type="MARKET", tag=f"COMBO_{leg['symbol'][:4]}_{leg['option_type']}_{idx}"[:20], user_id=self.user_id,
        )
        status = "DRY_RUN" if self.broker.dry_run else ("PLACED" if order_id else "FAILED")
        self.audit.record(
            event_type="ORDER_PLACED" if status != "FAILED" else "ORDER_FAILED",
            symbol=leg["symbol"], instrument_token=leg["instrument_token"], option_type=leg["option_type"],
            strike=leg["strike"], quantity=leg["quantity"], order_id=order_id or "DRY_RUN", status=status,
        )
        return order_id

    def _unwind_leg(self, leg: dict, idx: int) -> None:
        """Square off a leg that filled when a companion leg (possibly on the OTHER symbol) failed — same discipline as RuleBasedStrategy._unwind_leg, single attempt (the caller already logs/alerts on any remaining failure)."""
        opposite = self.broker.place_buy_order if leg["action"] == "SELL" else self.broker.place_sell_order
        try:
            self.rate_limiter.acquire()
            buyback_id = opposite(
                instrument_token=leg["instrument_token"], quantity=leg["quantity"], product=_PRODUCT,
                order_type="MARKET", tag=f"COMBO_UNWIND_{idx}"[:20], user_id=self.user_id,
            )
            self.audit.record(
                event_type="AUTO_UNWIND", symbol=leg["symbol"], instrument_token=leg["instrument_token"],
                option_type=leg["option_type"], strike=leg["strike"], quantity=leg["quantity"],
                order_id=buyback_id or "DRY_RUN", status="UNWOUND",
            )
        except Exception as exc:
            log.critical(
                "[AUTO-UNWIND FAILED] Could not square off weekend-combo leg %d (%s %s): %s — MANUAL INTERVENTION REQUIRED.",
                idx, leg["symbol"], leg["instrument_token"], exc,
            )
            self.audit.record(
                event_type="AUTO_UNWIND_FAILED", symbol=leg["symbol"], instrument_token=leg["instrument_token"],
                option_type=leg["option_type"], strike=leg["strike"], quantity=leg["quantity"],
                status="MANUAL_INTERVENTION_REQUIRED", note=str(exc),
            )

    def enter(self, bias: str) -> list[dict]:
        """
        Resolve + place all six legs across both symbols. SELL legs go
        first (across BOTH symbols together, not per-symbol) so the
        premium collected funds margin for the BUY legs placed after —
        same reasoning as RuleBasedStrategy.execute(). Any leg failing
        after at least one other filled triggers an immediate square-off
        of everything that did fill, and raises. Returns the filled legs
        (each with order_id/entry_price added) on full success.
        """
        legs = self.resolve_legs(bias)
        order = sorted(range(len(legs)), key=lambda i: 0 if legs[i]["action"] == "SELL" else 1)

        filled: list[dict] = []
        failed_idx: int | None = None
        for idx in order:
            leg = legs[idx]
            order_id = self._place_leg(leg, idx)
            if self.broker.dry_run or order_id:
                leg["order_id"] = order_id
                fill_price = self.broker.get_fill_price(order_id) if order_id else None
                leg["entry_price"] = fill_price if fill_price is not None else self.broker.get_ltp(leg["instrument_token"])
                filled.append(leg)
            else:
                failed_idx = idx
                break

        if failed_idx is not None and not self.broker.dry_run:
            for leg in filled:
                self._unwind_leg(leg, legs.index(leg))
            raise RuntimeError(
                f"Weekend combo leg {failed_idx} ({legs[failed_idx]['symbol']}) failed to fill — "
                f"{len(filled)} already-filled leg(s) were squared off automatically."
            )

        return sorted(filled, key=lambda leg: legs.index(leg))
