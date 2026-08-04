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

from automate.broker.base_broker import BaseBroker
from automate.compliance.sebi_rules import AuditTrail, ComplianceError, KillSwitch, OrderRateLimiter
from automate.strategies.common.base_strategy import BaseStrategy
from automate.utils.logger import get_logger
from automate.utils.option_utils import (
    find_instrument_token,
    find_nearest_expiry_by_type,
    round_to_nearest_strike,
)

log = get_logger(__name__)


_PREMIUM_RESOLVED_MODES = {"PREMIUM_OFFSET", "PREMIUM_BAND"}
# How far outward (in strike_step increments) PREMIUM_BAND will search
# before refusing to guess — generous enough to reach genuinely deep OTM
# strikes on any index/stock without an unbounded scan.
_PREMIUM_BAND_MAX_STEPS = 40


def resolve_leg_strike(leg: dict, spot_price: float, strike_step: float) -> float:
    """
    Turn a leg's strike_selection rule into an actual tradeable strike.

    OTM is directional: for a CE (call), "out of the money" means ABOVE
    spot; for a PE (put), it means BELOW spot. FIXED and ATM are the same
    regardless of option_type.

    PREMIUM_OFFSET/PREMIUM_BAND are NOT handled here — they need a live
    option chain + broker LTPs (this function is deliberately pure/no-
    broker, same as every other option_utils-style helper), so
    RuleBasedStrategy._resolve_leg routes those two modes to
    _resolve_premium_offset_strike/_resolve_premium_band_strike instead.
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
        strike_step: float | None = None,
        product: str = "NRML",
        user_id: int | None = None,
    ) -> None:
        super().__init__(broker, audit, kill_switch, rate_limiter)
        if not rules or not rules.get("legs"):
            raise ValueError("RuleBasedStrategy requires a non-empty `rules` dict with at least one leg.")

        self.symbol = symbol.upper()
        self.rules = rules
        self.product = product.upper()
        self.user_id = user_id  # owning CustomStrategy's user_id, for notify() scoping — see utils/notify.py

        # OPTION legs need a real strike_step/lot_size resolved up front
        # (wrong quantity/strike is a real-money risk — never guessed, see
        # below); an EQUITY-only strategy (no OPTION legs at all) has
        # neither concept and shouldn't fail __init__ over a lookup it'll
        # never actually use — e.g. a small-cap with no listed F&O
        # contracts is still perfectly tradable as a plain equity leg.
        has_option_legs = any((leg.get("instrument_type") or "OPTION") == "OPTION" for leg in rules["legs"])

        if not has_option_legs:
            self.strike_step = None
            self.real_lot_size = 1  # equity: 1 unit = 1 share, no F&O lot concept
        else:
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

        # Which legs to (re)enter this call — None (default) means "all of
        # them," today's only behavior. Set via run(leg_indices=[...]) by
        # custom_strategy_scheduler.py when a calendar spread's legs need
        # to roll over on independent cycles (see _get_last_entered_expiry
        # there) — one leg's expiry rolling doesn't mean the other leg's
        # basket needs re-entering too.
        self._leg_indices: list[int] | None = None

        log.info(
            "RuleBasedStrategy | symbol=%s | legs=%d | strike_step=%s | product=%s",
            self.symbol, len(rules["legs"]), self.strike_step, self.product,
        )

    def run(self, leg_indices: list[int] | None = None) -> dict:
        """Same lifecycle wrapper as BaseStrategy.run() — leg_indices restricts execute()/preview() to a subset of legs (see __init__ docstring above)."""
        self._leg_indices = leg_indices
        return super().run()

    # ------------------------------------------------------------------

    def _fetch_spot_price(self) -> float:
        ltp = self.broker.get_ltp(self.instrument_key)
        if ltp is None or ltp <= 0:
            raise RuntimeError(f"Invalid/missing LTP for '{self.symbol}'.")
        return ltp

    def _active_legs(self) -> list[tuple[int, dict]]:
        """(original_index, leg) pairs for the legs this call should act on — see run()'s leg_indices."""
        legs = self.rules["legs"]
        if self._leg_indices is None:
            return list(enumerate(legs))
        return [(i, legs[i]) for i in self._leg_indices]

    def _leg_expiry_mode(self, leg: dict) -> str:
        """A leg's own expiry_mode overrides the strategy default — this is what makes a calendar spread (legs at different expiries) possible. See rule_schema.py."""
        return leg.get("expiry_mode") or (self.rules.get("expiry") or {}).get("mode", "WEEKLY")

    def _resolve_expiries_and_chains(self, legs: list[dict]) -> tuple[dict, dict]:
        """
        Resolve the nearest expiry for each DISTINCT expiry_mode these legs
        need (usually just one — the strategy default — unless this is a
        calendar spread), and fetch each resulting expiry's option chain
        once, even if several legs share it. EQUITY legs need neither (no
        expiry/chain concept) and are excluded — an all-EQUITY leg set
        returns ({}, {}) without any broker call at all.

        Returns (mode -> resolved expiry date string, expiry date -> chain_data).
        """
        option_legs = [leg for leg in legs if (leg.get("instrument_type") or "OPTION") == "OPTION"]
        if not option_legs:
            return {}, {}
        modes_needed = {self._leg_expiry_mode(leg) for leg in option_legs}
        expiries = self.broker.get_option_contracts(self.instrument_key)
        if not expiries:
            raise RuntimeError(f"No option expiries returned for '{self.symbol}'.")
        now = self.broker.get_current_time()

        mode_to_expiry: dict = {}
        for mode in modes_needed:
            nearest = find_nearest_expiry_by_type(expiries, mode, reference_date=now.date() if now else None)
            if not nearest:
                raise RuntimeError(f"Could not determine nearest {mode.lower()} expiry for '{self.symbol}'.")
            mode_to_expiry[mode] = nearest

        expiry_to_chain: dict = {}
        for expiry in set(mode_to_expiry.values()):
            chain = self.broker.get_option_chain(self.instrument_key, expiry)
            if not chain:
                raise RuntimeError(f"Empty option chain for {self.symbol} expiry {expiry}.")
            expiry_to_chain[expiry] = chain

        return mode_to_expiry, expiry_to_chain

    def _resolve_quantity(self, leg: dict, spot_price: float, token: str, transaction_type: str, lot_size: int | None = None) -> int:
        """
        Today's default: `leg["lots"] * lot_size`, fixed (lot_size defaults
        to self.real_lot_size — the real F&O lot; an EQUITY leg passes 1,
        since its "lots" field means raw shares, not an F&O multiple — see
        rule_schema.py). If the leg opts into RISK_PCT sizing
        (rule_schema.py), size it instead so its capital cost is <=
        risk_pct% of the account's CURRENT available balance — SELL legs
        use the REAL broker-calculated margin when available
        (utils/margin.py::resolve_required_margin — the same Upstox Margin
        Calculator figure a live order would actually need, not a
        flat-percentage guess; for an EQUITY leg, resolve_required_margin
        naturally falls back to its flat estimate function's spot*qty*rate
        shape only when the broker has no margin figure — a plain equity
        BUY/SELL isn't a margin product at all, so RISK_PCT sizing for
        EQUITY BUY legs instead uses the actual share price like any other
        BUY below). Refuses (raises) rather than silently under/oversizing
        if even 1 lot exceeds the budget — same "wrong quantity is a
        real-money risk, don't guess" discipline __init__ already applies
        to lot_size/strike_step above.
        """
        lot_size = self.real_lot_size if lot_size is None else lot_size
        sizing = leg.get("sizing")
        if not sizing or sizing.get("mode") != "RISK_PCT":
            return leg["lots"] * lot_size

        if self.user_id is None:
            raise RuntimeError(
                f"{self.symbol}: RISK_PCT sizing requires a real owning user_id (for the capital lookup) — "
                f"not available in this call context."
            )

        from automate.utils.margin import (
            INDEX_SYMBOLS,
            is_commodity_instrument_key,
            resolve_required_margin,
        )
        from automate.utils.wallet import get_wallet_summary

        available = get_wallet_summary(self.user_id)["available_balance"]
        budget = available * (sizing["risk_pct"] / 100.0)

        is_equity_leg = (leg.get("instrument_type") or "OPTION") == "EQUITY"
        if transaction_type == "SELL" and not is_equity_leg:
            per_lot_cost = resolve_required_margin(
                self.broker, token, lot_size, "SELL", spot_price,
                self.symbol in INDEX_SYMBOLS, is_commodity_instrument_key(self.instrument_key),
            )
        else:
            premium = self.broker.get_ltp(token)
            if premium is None:
                raise RuntimeError(f"{self.symbol}: could not fetch a current price for {token} to size this leg by risk %.")
            per_lot_cost = premium * lot_size

        if per_lot_cost <= 0:
            raise RuntimeError(f"{self.symbol}: computed a non-positive per-lot cost ({per_lot_cost}) — refusing to size this leg.")

        max_lots = int(budget // per_lot_cost)
        if max_lots < 1:
            raise ComplianceError(
                f"{self.symbol}: risking {sizing['risk_pct']}% of available capital (₹{budget:,.2f}) isn't enough "
                f"for even 1 lot (₹{per_lot_cost:,.2f}/lot) — entry refused rather than sized to 0 or oversized."
            )
        return max_lots * lot_size

    def _resolve_equity_leg(self, leg: dict, spot_price: float) -> dict:
        """Return the resolved-leg dict for a plain cash EQUITY leg — no strike/expiry/option_type, just BUY/SELL N shares of the underlying itself."""
        quantity = self._resolve_quantity(leg, spot_price, self.instrument_key, leg["action"], lot_size=1)
        return {
            "instrument_token": self.instrument_key,
            "instrument_type": "EQUITY",
            "option_type": None,
            "strike": None,
            "expiry": None,
            "quantity": quantity,
            "transaction_type": leg["action"],
        }

    def _resolve_premium_offset_strike(self, leg: dict, spot_price: float, chain_data: list) -> float:
        """
        PREMIUM_OFFSET (see rule_schema.py): offset = round_to_nearest_strike
        (live ATM straddle premium / divisor, strike_step). Always reads the
        ATM CE+PE premiums straight off THIS leg's own chain_data — not
        from any other leg's resolved state — so it works regardless of
        whether the strategy actually has its own ATM legs, and has no
        leg-resolution-order dependency.
        """
        atm_strike = round_to_nearest_strike(spot_price, self.strike_step)
        ce_token = find_instrument_token(chain_data, atm_strike, "CE")
        pe_token = find_instrument_token(chain_data, atm_strike, "PE")
        if not ce_token or not pe_token:
            raise ComplianceError(
                f"{self.symbol}: no listed ATM {atm_strike} CE/PE contract to price a PREMIUM_OFFSET leg from."
            )
        ce_premium = self.broker.get_ltp(ce_token)
        pe_premium = self.broker.get_ltp(pe_token)
        if not ce_premium or not pe_premium:
            raise RuntimeError(
                f"{self.symbol}: could not fetch live ATM {atm_strike} CE/PE premiums for a PREMIUM_OFFSET leg — refusing to guess."
            )
        divisor = float(leg["strike_selection"]["value"])
        offset = round_to_nearest_strike((ce_premium + pe_premium) / divisor, self.strike_step)
        sign = 1 if leg["option_type"] == "CE" else -1
        return atm_strike + sign * offset

    def _resolve_premium_band_strike(self, leg: dict, spot_price: float, chain_data: list) -> float:
        """
        PREMIUM_BAND (see rule_schema.py): the nearest strike, searching
        outward from ATM in this leg's OTM direction, whose own live
        premium falls within [min, max] (₹) — a proxy for "pick a strike
        by how much it costs" (e.g. a deep-OTM insurance leg trading
        ₹40-70) when no live delta feed is wired up. Refuses (raises)
        rather than falling back to the nearest miss if nothing in the
        search window matches — same "wrong strike is a real-money risk,
        don't guess" discipline as everywhere else in this class.
        """
        sel = leg["strike_selection"]
        band_min, band_max = float(sel["min"]), float(sel["max"])
        atm_strike = round_to_nearest_strike(spot_price, self.strike_step)
        sign = 1 if leg["option_type"] == "CE" else -1
        for step_n in range(1, _PREMIUM_BAND_MAX_STEPS + 1):
            candidate = atm_strike + sign * step_n * self.strike_step
            token = find_instrument_token(chain_data, candidate, leg["option_type"])
            if not token:
                continue
            premium = self.broker.get_ltp(token)
            if premium is not None and band_min <= premium <= band_max:
                return candidate
        raise ComplianceError(
            f"{self.symbol}: no {leg['option_type']} strike within {_PREMIUM_BAND_MAX_STEPS} strikes of ATM "
            f"{atm_strike} has a live premium between ₹{band_min} and ₹{band_max} — refusing to guess."
        )

    def _resolve_leg(self, leg: dict, spot_price: float, expiry: str | None, chain_data: list | None) -> dict:
        """Return {instrument_token, strike, quantity, transaction_type, tag, ...leg metadata}."""
        if (leg.get("instrument_type") or "OPTION") == "EQUITY":
            return self._resolve_equity_leg(leg, spot_price)
        strike_mode = leg["strike_selection"]["mode"]
        if strike_mode == "PREMIUM_OFFSET":
            strike = self._resolve_premium_offset_strike(leg, spot_price, chain_data)
        elif strike_mode == "PREMIUM_BAND":
            strike = self._resolve_premium_band_strike(leg, spot_price, chain_data)
        else:
            strike = resolve_leg_strike(leg, spot_price, self.strike_step)
        token = find_instrument_token(chain_data, strike, leg["option_type"])
        if not token:
            raise ComplianceError(
                f"No listed contract for {self.symbol} {strike} {leg['option_type']} expiry {expiry}."
            )
        quantity = self._resolve_quantity(leg, spot_price, token, leg["action"])
        return {
            "instrument_token": token,
            "instrument_type": "OPTION",
            "option_type": leg["option_type"],
            "strike": strike,
            "expiry": expiry,
            "quantity": quantity,
            "transaction_type": leg["action"],
        }

    def _run_pre_trade_checks(self, resolved_pairs: list[tuple[int, dict]], spot_price: float) -> None:
        """
        SEBI pre-trade compliance for every leg about to be placed —
        ±20% circuit price-band check (options only, a strike has no
        meaning for an equity leg) and NSE freeze-quantity check, via
        compliance/sebi_rules.py. Raises ComplianceError on a price-band
        breach; the freeze-quantity check only logs (Upstox's
        place_*_order already sets slice=True, auto-splitting a
        freeze-exceeding order at the broker level — see
        validate_order_quantity's own docstring).
        """
        from automate.compliance.sebi_rules import validate_order_quantity, validate_price_band

        for _, resolved in resolved_pairs:
            if resolved["instrument_type"] == "OPTION" and resolved.get("strike") is not None:
                try:
                    validate_price_band(resolved["strike"], spot_price)
                except ValueError as exc:
                    raise ComplianceError(str(exc)) from exc
            validate_order_quantity(self.symbol, resolved["quantity"])

    def _place_leg(self, resolved: dict, idx: int) -> str | None:
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
        result: dict = {"status": "failed", "symbol": self.symbol, "spot_price": None, "expiry": None, "expiries": {}, "legs": []}

        spot_price = self._fetch_spot_price()
        result["spot_price"] = spot_price

        active = self._active_legs()
        mode_to_expiry, expiry_to_chain = self._resolve_expiries_and_chains([leg for _, leg in active])
        result["expiries"] = mode_to_expiry
        # Kept for backward compat with callers reading a single "expiry"
        # (routes_custom_strategies.py's payoff/greeks preview, backtest
        # engine) — the strategy-DEFAULT mode's resolved date. Meaningless
        # as "the" expiry for a true calendar spread; those callers should
        # move to per-leg `expiry` (already on each resolved leg below).
        default_mode = (self.rules.get("expiry") or {}).get("mode", "WEEKLY")
        result["expiry"] = mode_to_expiry.get(default_mode)

        resolved_legs = []
        for _, leg in active:
            if (leg.get("instrument_type") or "OPTION") == "EQUITY":
                resolved = self._resolve_leg(leg, spot_price, None, None)
            else:
                expiry = mode_to_expiry[self._leg_expiry_mode(leg)]
                resolved = self._resolve_leg(leg, spot_price, expiry, expiry_to_chain[expiry])
            resolved["current_price"] = self.broker.get_ltp(resolved["instrument_token"])
            resolved_legs.append(resolved)

        result["legs"] = resolved_legs
        result["status"] = "success"
        return result

    def execute(self) -> dict:
        result: dict = {
            "status": "failed",
            "symbol": self.symbol,
            "spot_price": None,
            "expiry": None,
            "expiries": {},
            "legs": [],
            "dry_run": self.broker.dry_run,
        }

        spot_price = self._fetch_spot_price()
        result["spot_price"] = spot_price

        active = self._active_legs()  # [(original_leg_index, leg), ...] — see run(leg_indices=...)
        mode_to_expiry, expiry_to_chain = self._resolve_expiries_and_chains([leg for _, leg in active])
        result["expiries"] = mode_to_expiry
        default_mode = (self.rules.get("expiry") or {}).get("mode", "WEEKLY")
        result["expiry"] = mode_to_expiry.get(default_mode)  # see preview()'s comment on this field's limits for calendar spreads

        # (original_leg_index, resolved_leg_dict) pairs, in ACTIVE order —
        # original_leg_index is what gets used for order tags/audit/unwind
        # so it stays stable and traceable even when only a SUBSET of legs
        # is being (re)entered this call.
        resolved_pairs: list[tuple[int, dict]] = []
        for original_idx, leg in active:
            if (leg.get("instrument_type") or "OPTION") == "EQUITY":
                resolved = self._resolve_leg(leg, spot_price, None, None)
            else:
                expiry = mode_to_expiry[self._leg_expiry_mode(leg)]
                resolved = self._resolve_leg(leg, spot_price, expiry, expiry_to_chain[expiry])
            resolved_pairs.append((original_idx, resolved))

        # SEBI pre-trade checks — BEFORE any order is placed, so a failing
        # leg aborts the whole basket instead of triggering an auto-unwind
        # of already-filled legs for something that should never have been
        # attempted at all. This used to only ever run for the retired
        # hand-written TenPercentOTMStrangle strategy (hardcoded to its
        # exact 2-leg shape) — every custom-builder strategy (paper AND
        # live) skipped both checks entirely. warn_position_limits() only
        # logs (never raises) so it isn't called here — it's advisory, not
        # a hard gate, same as its own docstring says.
        self._run_pre_trade_checks(resolved_pairs, spot_price)

        # SELL legs first — collected premium is available as margin before any BUY legs need it.
        order = sorted(range(len(resolved_pairs)), key=lambda pos: 0 if resolved_pairs[pos][1]["transaction_type"] == "SELL" else 1)

        filled: list[tuple[int, dict, str]] = []
        failed_idx: int | None = None
        for pos in order:
            original_idx, resolved = resolved_pairs[pos]
            order_id = self._place_leg(resolved, original_idx)
            if self.broker.dry_run or order_id:
                resolved["order_id"] = order_id
                # Prefer the broker's own reported fill price over a fresh
                # LTP snapshot — LTP can move between order placement and
                # this call (slippage, a fast market), so it isn't
                # necessarily what the account actually paid/received.
                # Falls back to LTP for dry runs (order_id is None, nothing
                # actually filled) or if the broker doesn't know the fill
                # yet (e.g. UpstoxBroker before the exchange confirms).
                fill_price = self.broker.get_fill_price(order_id) if order_id else None
                resolved["entry_price"] = fill_price if fill_price is not None else self.broker.get_ltp(resolved["instrument_token"])
                filled.append((original_idx, resolved, order_id or "DRY_RUN"))
            else:
                failed_idx = original_idx
                break

        if failed_idx is not None and not self.broker.dry_run:
            for idx, resolved, _order_id in filled:
                self._unwind_leg(resolved, idx)
            raise RuntimeError(
                f"Custom strategy leg {failed_idx} failed to fill for {self.symbol} — "
                f"{len(filled)} already-filled leg(s) were squared off automatically."
            )

        result["legs"] = [resolved for _, resolved, _ in sorted(filled, key=lambda t: t[0])]
        result["leg_indices"] = [idx for idx, _, _ in sorted(filled, key=lambda t: t[0])]
        result["status"] = "dry_run" if self.broker.dry_run else "success"
        log.info("Custom strategy complete | %s | legs=%d | status=%s", self.symbol, len(result["legs"]), result["status"])
        return result
