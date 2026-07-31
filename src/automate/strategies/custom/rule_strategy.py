"""
strategies/custom/rule_strategy.py — generic executor for user-built
custom strategies (see rule_schema.py for the JSON contract).

This is the interpreter half of the strategy builder: instead of hardcoding
"sell CE 10% OTM + sell PE 10% OTM" in Python, RuleBasedStrategy reads that
same shape (and any other leg combination) out of a `rules` dict AT
RUNTIME — the rules themselves live only in the custom_strategies table
(rules_json column), never in a file. It plugs into the exact same
BaseStrategy/broker contract as every strategy, so it works unmodified
with MockBroker (backtest), PaperBroker (paper trading) and UpstoxBroker
(live).

Unlike a fixed strangle's basket-sell (all legs same side), a user-built
strategy can freely mix BUY and SELL legs (e.g. an Iron Condor or a debit
spread) — so legs are placed sequentially here (SELL legs first, so
premium collected funds margin for any BUY legs placed after). Any leg
failing after at least one other leg filled triggers an immediate
square-off of everything that did fill, generalized to N legs of mixed
direction.
"""
from typing import Optional

from automate.strategies.common.base_strategy import BaseStrategy
from automate.broker.base_broker import BaseBroker
from automate.compliance.sebi_rules import AuditTrail, ComplianceError, KillSwitch, OrderRateLimiter
from automate.utils.logger import get_logger
from automate.utils.option_utils import find_instrument_token, find_nearest_expiry_by_type, round_to_nearest_strike

log = get_logger(__name__)


def resolve_leg_strike(leg: dict, spot_price: float, strike_step: float) -> float:
    """
    Turn a leg's strike_selection rule into an actual tradeable strike.

    OTM is directional: for a CE (call), "out of the money" means ABOVE
    spot; for a PE (put), it means BELOW spot. FIXED and ATM are the same
    regardless of option_type.
    """
    sel = leg["strike_selection"]
    mode = sel["mode"]
    option_type = leg["option_type"]

    if mode == "ATM":
        return round_to_nearest_strike(spot_price, strike_step)
    if mode == "FIXED":
        return round_to_nearest_strike(float(sel["value"]), strike_step)

    value = float(sel["value"])
    sign = 1 if option_type == "CE" else -1
    if mode == "OTM_PERCENT":
        raw = spot_price * (1 + sign * value / 100.0)
    elif mode == "OTM_POINTS":
        raw = spot_price + sign * value
    else:
        raise ValueError(f"Unknown strike_selection mode: {mode}")
    return round_to_nearest_strike(raw, strike_step)


class RuleBasedStrategy(BaseStrategy):
    """
    Executes any N-leg strategy described by `rules` (see rule_schema.py).

    Args mirror the old TenPercentOTMStrangle constructor where they
    overlap (symbol/strike_step/product) plus:

        rules: dict — validated (rule_schema.validate_rules) rule
            definition. Required.
    """

    def __init__(
        self,
        broker: BaseBroker,
        audit: AuditTrail,
        kill_switch: KillSwitch,
        rate_limiter: OrderRateLimiter,
        symbol: str,
        rules: dict,
        strike_step: Optional[float] = None,
        product: str = "NRML",
        user_id: Optional[int] = None,
    ) -> None:
        super().__init__(broker, audit, kill_switch, rate_limiter)
        if not rules or not rules.get("legs"):
            raise ValueError("RuleBasedStrategy requires a non-empty `rules` dict with at least one leg.")

        self.symbol = symbol.upper()
        self.rules = rules
        self.product = product.upper()
        self.user_id = user_id  # owning CustomStrategy's user_id, for notify() scoping — see utils/notify.py

        if strike_step is not None:
            self.strike_step = strike_step
        else:
            real_step = broker.get_strike_step(self.symbol)
            if real_step is None:
                raise RuntimeError(
                    f"Could not resolve a real strike step for '{self.symbol}' — refusing to guess."
                )
            self.strike_step = real_step

        real_lot_size = broker.get_lot_size(self.symbol)
        if real_lot_size is None:
            raise RuntimeError(
                f"Could not resolve a real lot size for '{self.symbol}' from {type(broker).__name__}'s "
                f"instrument master — refusing to guess (wrong quantity is a real-money risk)."
            )
        self.real_lot_size = real_lot_size

        self.instrument_key: str = broker.resolve_instrument_key(self.symbol)

        log.info(
            "RuleBasedStrategy | symbol=%s | legs=%d | strike_step=%s | product=%s",
            self.symbol, len(rules["legs"]), self.strike_step, self.product,
        )

    # ------------------------------------------------------------------

    def _fetch_spot_price(self) -> float:
        ltp = self.broker.get_ltp(self.instrument_key)
        if ltp is None or ltp <= 0:
            raise RuntimeError(f"Invalid/missing LTP for '{self.symbol}'.")
        return ltp

    def _get_nearest_expiry(self) -> str:
        expiries = self.broker.get_option_contracts(self.instrument_key)
        if not expiries:
            raise RuntimeError(f"No option expiries returned for '{self.symbol}'.")
        now = self.broker.get_current_time()
        expiry_mode = (self.rules.get("expiry") or {}).get("mode", "WEEKLY")
        nearest = find_nearest_expiry_by_type(expiries, expiry_mode, reference_date=now.date() if now else None)
        if not nearest:
            raise RuntimeError(f"Could not determine nearest {expiry_mode.lower()} expiry for '{self.symbol}'.")
        return nearest

    def _resolve_leg(self, leg: dict, spot_price: float, expiry: str, chain_data: list) -> dict:
        """Return {instrument_token, strike, quantity, transaction_type, tag, ...leg metadata}."""
        quantity = leg["lots"] * self.real_lot_size
        strike = resolve_leg_strike(leg, spot_price, self.strike_step)
        token = find_instrument_token(chain_data, strike, leg["option_type"])
        if not token:
            raise ComplianceError(
                f"No listed contract for {self.symbol} {strike} {leg['option_type']} expiry {expiry}."
            )
        return {
            "instrument_token": token,
            "instrument_type": "OPTION",
            "option_type": leg["option_type"],
            "strike": strike,
            "expiry": expiry,
            "quantity": quantity,
            "transaction_type": leg["action"],
        }

    def _place_leg(self, resolved: dict, idx: int) -> Optional[str]:
        self.rate_limiter.acquire()
        place = self.broker.place_sell_order if resolved["transaction_type"] == "SELL" else self.broker.place_buy_order
        self.audit.record(
            event_type="ORDER_INITIATED",
            symbol=self.symbol,
            instrument_token=resolved["instrument_token"],
            option_type=resolved["option_type"] or "EQ",
            strike=resolved["strike"] or 0,
            quantity=resolved["quantity"],
            status="PENDING",
            note=f"custom_leg_{idx} action={resolved['transaction_type']}",
        )
        order_id = place(
            instrument_token=resolved["instrument_token"],
            quantity=resolved["quantity"],
            product=self.product,
            order_type="MARKET",
            tag=f"CUSTOM_{resolved['option_type']}_{self.symbol[:6]}_{idx}"[:20],
            user_id=self.user_id,
        )
        status = "DRY_RUN" if self.broker.dry_run else ("PLACED" if order_id else "FAILED")

        # Post-order status reconciliation check (SEBI/exchange rejection handling)
        if order_id and not self.broker.dry_run:
            status_check = self.broker.get_order_status(order_id)
            if status_check in ("rejected", "cancelled", "failed"):
                log.error("Order %s was rejected/failed by the exchange.", order_id)
                from automate.utils.notify import notify
                notify(
                    "custom_strategy",
                    f"{self.symbol} {resolved['option_type']} {resolved['strike']} order was {status_check} by "
                    f"the exchange (order_id={order_id}). Common causes: insufficient margin/funds, price band, "
                    f"or the contract no longer being tradeable.",
                    user_id=self.user_id,
                )
                self.audit.record(
                    event_type="ORDER_FAILED",
                    symbol=self.symbol,
                    instrument_token=resolved["instrument_token"],
                    option_type=resolved["option_type"] or "EQ",
                    strike=resolved["strike"] or 0,
                    quantity=resolved["quantity"],
                    order_id=order_id,
                    status="REJECTED",
                    note=f"Reconciliation check: {status_check}",
                )
                return None

        self.audit.record(
            event_type="ORDER_PLACED" if status != "FAILED" else "ORDER_FAILED",
            symbol=self.symbol,
            instrument_token=resolved["instrument_token"],
            option_type=resolved["option_type"] or "EQ",
            strike=resolved["strike"] or 0,
            quantity=resolved["quantity"],
            order_id=order_id or "DRY_RUN",
            status=status,
        )
        return order_id

    _UNWIND_MAX_RETRIES = 3
    _UNWIND_RETRY_DELAY_SEC = 2.0

    def _unwind_leg(self, resolved: dict, idx: int) -> None:
        """Square off a leg that filled when a companion leg failed — opposite transaction, same quantity."""
        from pathlib import Path
        opposite = self.broker.place_buy_order if resolved["transaction_type"] == "SELL" else self.broker.place_sell_order
        
        last_exc = None
        for attempt in range(1, self._UNWIND_MAX_RETRIES + 1):
            try:
                self.rate_limiter.acquire()
                buyback_id = opposite(
                    instrument_token=resolved["instrument_token"],
                    quantity=resolved["quantity"],
                    product=self.product,
                    order_type="MARKET",
                    tag=f"UNWIND_{resolved['option_type']}_{self.symbol[:6]}_{idx}"[:20],
                    user_id=self.user_id,
                )
                self.audit.record(
                    event_type="AUTO_UNWIND",
                    symbol=self.symbol,
                    instrument_token=resolved["instrument_token"],
                    option_type=resolved["option_type"] or "EQ",
                    strike=resolved["strike"] or 0,
                    quantity=resolved["quantity"],
                    order_id=buyback_id or "DRY_RUN",
                    status="UNWOUND",
                )
                log.info("Auto-unwound leg %d successfully on attempt %d.", idx, attempt)
                return
            except Exception as exc:
                last_exc = exc
                log.warning(
                    "Auto-unwind attempt %d of %d failed for leg %d (%s) of %s: %s",
                    attempt, self._UNWIND_MAX_RETRIES, idx, resolved["instrument_token"], self.symbol, exc
                )
                if attempt < self._UNWIND_MAX_RETRIES:
                    import time
                    time.sleep(self._UNWIND_RETRY_DELAY_SEC)

        log.critical(
            "[AUTO-UNWIND FAILED] Could not square off leg %d (%s) for %s after %d retries: %s — MANUAL INTERVENTION REQUIRED.",
            idx, resolved["instrument_token"], self.symbol, self._UNWIND_MAX_RETRIES, last_exc,
        )
        from automate.utils.notify import notify
        notify(
            "custom_strategy",
            f"MANUAL INTERVENTION REQUIRED — could not square off {self.symbol} "
            f"{resolved['option_type']} {resolved['strike']} after {self._UNWIND_MAX_RETRIES} attempts: {last_exc}. "
            f"A naked/unhedged position may be open on your real account — check it now.",
            user_id=self.user_id,
        )
        self.audit.record(
            event_type="AUTO_UNWIND_FAILED",
            symbol=self.symbol,
            instrument_token=resolved["instrument_token"],
            option_type=resolved["option_type"] or "EQ",
            strike=resolved["strike"] or 0,
            quantity=resolved["quantity"],
            status="MANUAL_INTERVENTION_REQUIRED",
            note=str(last_exc),
        )

        try:
            from datetime import datetime
            alert_dir = Path("logs")
            alert_dir.mkdir(parents=True, exist_ok=True)
            alert_path = alert_dir / f"ALERT_MANUAL_INTERVENTION_{self.symbol}_{datetime.now().strftime('%Y%m%dT%H%M%S')}.flag"
            alert_path.write_text(
                f"MANUAL INTERVENTION REQUIRED\n"
                f"Auto-unwind failed for leg {idx} of {self.symbol}.\n"
                f"Instrument: {resolved['instrument_token']}\n"
                f"Direction: {resolved['transaction_type']}\n"
                f"Quantity: {resolved['quantity']}\n"
                f"Error: {last_exc}\n"
            )
            log.info("Wrote manual intervention alert flag to %s", alert_path)
        except Exception as write_exc:
            log.error("Failed to write manual intervention alert file: %s", write_exc)

    # ------------------------------------------------------------------

    def preview(self) -> dict:
        """
        Resolve every leg (spot, expiry, strikes, current LTP) exactly
        like execute() does, but NEVER places an order — read-only. Used
        to show a strategy's current expected payoff/Greeks before it's
        ever been backtested or deployed (see api/routes_custom_strategies.py's
        payoff/greeks-preview endpoints), so a DRAFT strategy can still
        show real numbers instead of nothing.

        Returns the same {status, symbol, spot_price, expiry, legs: [...]}
        shape as execute(), except each leg dict has "current_price"
        instead of "entry_price"/"order_id" (nothing was actually bought
        or sold).
        """
        result: dict = {"status": "failed", "symbol": self.symbol, "spot_price": None, "expiry": None, "legs": []}

        spot_price = self._fetch_spot_price()
        result["spot_price"] = spot_price

        expiry = self._get_nearest_expiry()
        result["expiry"] = expiry
        chain_data = self.broker.get_option_chain(self.instrument_key, expiry)
        if not chain_data:
            raise RuntimeError(f"Empty option chain for {self.symbol} expiry {expiry}.")

        resolved_legs = [self._resolve_leg(leg, spot_price, expiry, chain_data) for leg in self.rules["legs"]]
        for resolved in resolved_legs:
            resolved["current_price"] = self.broker.get_ltp(resolved["instrument_token"])

        result["legs"] = resolved_legs
        result["status"] = "success"
        return result

    def execute(self) -> dict:
        result: dict = {
            "status": "failed",
            "symbol": self.symbol,
            "spot_price": None,
            "expiry": None,
            "legs": [],
            "dry_run": self.broker.dry_run,
        }

        spot_price = self._fetch_spot_price()
        result["spot_price"] = spot_price

        expiry = self._get_nearest_expiry()
        result["expiry"] = expiry
        chain_data = self.broker.get_option_chain(self.instrument_key, expiry)
        if not chain_data:
            raise RuntimeError(f"Empty option chain for {self.symbol} expiry {expiry}.")

        resolved_legs = [self._resolve_leg(leg, spot_price, expiry, chain_data) for leg in self.rules["legs"]]
        # SELL legs first — collected premium is available as margin before any BUY legs need it.
        order = sorted(range(len(resolved_legs)), key=lambda i: 0 if resolved_legs[i]["transaction_type"] == "SELL" else 1)

        filled: list[tuple[int, dict, str]] = []
        failed_idx: Optional[int] = None
        for i in order:
            resolved = resolved_legs[i]
            order_id = self._place_leg(resolved, i)
            if self.broker.dry_run or order_id:
                resolved["order_id"] = order_id
                resolved["entry_price"] = self.broker.get_ltp(resolved["instrument_token"])
                filled.append((i, resolved, order_id or "DRY_RUN"))
            else:
                failed_idx = i
                break

        if failed_idx is not None and not self.broker.dry_run:
            for idx, resolved, order_id in filled:
                self._unwind_leg(resolved, idx)
            raise RuntimeError(
                f"Custom strategy leg {failed_idx} failed to fill for {self.symbol} — "
                f"{len(filled)} already-filled leg(s) were squared off automatically."
            )

        result["legs"] = [resolved for _, resolved, _ in sorted(filled, key=lambda t: t[0])]
        result["status"] = "dry_run" if self.broker.dry_run else "success"
        log.info("Custom strategy complete | %s | legs=%d | status=%s", self.symbol, len(result["legs"]), result["status"])
        return result
