"""
broker/paper_broker.py — Paper Trading Broker

Wraps a real live broker to fetch actual market data, but intercepts
order placement to simulate a fill (with slippage) instead of executing it
for real. Doesn't keep its own separate log — the strategy layer's own
record of what happened (utils/position_tracker.py's `positions` table,
the single db for the whole bot) and the SEBI-mandated AuditTrail
(logs/audit_trail.log, records every order attempt including auto-unwinds)
already cover this; a third, PaperBroker-only leg-level SQLite log used to
exist here too (paper_trades.db) but was a redundant duplicate consumed by
nothing except the now-deleted run_paper_tracker.py.
"""
import uuid
from typing import Optional, List

from automate.broker.base_broker import BaseBroker
from automate.utils.logger import get_logger

log = get_logger(__name__)

class PaperBroker(BaseBroker):
    """
    Acts as a proxy to a real broker for data, but executes orders virtually.
    """
    def __init__(self, real_broker: BaseBroker, slippage_pct: float = 0.001):
        self.real_broker = real_broker
        self.slippage_pct = slippage_pct

        # place_sell_order() always records a (virtual) fill — it never skips
        # the way a live broker's dry-run path does — so this is False. The
        # strategy layer reads broker.dry_run directly to label its result/log
        # as "DRY_RUN" vs "PLACED"/"success".
        self.dry_run = False

    def get_ltp(self, instrument_key: str) -> Optional[float]:
        return self.real_broker.get_ltp(instrument_key)

    def get_ltp_batch(self, instrument_keys: List[str]) -> dict:
        return self.real_broker.get_ltp_batch(instrument_keys)

    def get_market_depth(self, instrument_key: str) -> Optional[dict]:
        # Real order-book snapshot from the live market, even in paper mode
        # (matches get_ltp's "real quotes, simulated fills" split) — a paper
        # account has no order book of its own to show.
        return self.real_broker.get_market_depth(instrument_key)


    def get_option_contracts(self, instrument_key: str) -> List[str]:
        return self.real_broker.get_option_contracts(instrument_key)
        
    def get_option_chain(self, instrument_key: str, expiry_date: str) -> list:
        return self.real_broker.get_option_chain(instrument_key, expiry_date)
        
    def refresh_instrument_master(self, force: bool = False) -> None:
        self.real_broker.refresh_instrument_master(force)
        
    def resolve_instrument_key(self, symbol: str) -> str:
        return self.real_broker.resolve_instrument_key(symbol)

    def get_lot_size(self, symbol: str) -> Optional[int]:
        return self.real_broker.get_lot_size(symbol)

    def get_strike_step(self, symbol: str) -> Optional[float]:
        return self.real_broker.get_strike_step(symbol)

    def get_required_margin(
        self, instrument_key: str, quantity: int, transaction_type: str, product: str = "D",
    ) -> Optional[float]:
        # Paper trading should validate against the SAME real margin
        # numbers a live order would need — delegates to the wrapped real
        # broker's Margin Calculator call, same "real quotes, simulated
        # fills" split get_ltp()/get_market_depth() already use.
        return self.real_broker.get_required_margin(instrument_key, quantity, transaction_type, product)

    def get_basket_required_margin(self, instruments: List[dict]) -> Optional[float]:
        return self.real_broker.get_basket_required_margin(instruments)

    def _place_order(
        self,
        transaction_type: str,
        instrument_token: str,
        quantity: int,
        product: str = "NRML",
        order_type: str = "MARKET",
        tag: str = "",
        user_id: Optional[int] = None,
    ) -> Optional[str]:
        """
        Simulate a paper fill: validates virtual balance/margin for both spot equities
        and options/futures, applies slippage, and generates a synthetic order_id.
        """
        current_price = self.get_ltp(instrument_token)
        if current_price is None:
            log.error("PaperBroker: Cannot place order for %s, no live LTP available.", instrument_token)
            return None

        # --- Real broker simulation: balance and margin validations ---
        from automate.utils.margin import resolve_required_margin, is_commodity_instrument_key, INDEX_SYMBOLS
        from automate.utils.wallet import get_wallet_summary
        import re

        # 1. Resolve instrument details from master cache — REFUSE the
        # order (never a fabricated guess) if this lookup fails. This used
        # to silently default to base_symbol="RELIANCE"/inst_type="EQUITY"
        # on any failure, which is a real-money-adjacent correctness bug
        # even in paper mode: a resolution glitch on, say, a NIFTY option
        # SELL would silently fall into the (checked-only-for-BUY) equity
        # branch below and skip margin validation ENTIRELY, or compute
        # margin/index-rate off the wrong underlying's price entirely.
        # "Refuse rather than guess" is this codebase's standing rule for
        # exactly this class of lookup (see lot_size/strike_step).
        base_symbol: Optional[str] = None
        inst_type: Optional[str] = None
        if hasattr(self.real_broker, "_cache"):
            try:
                df = self.real_broker._cache.get_or_refresh()
                matches = df[df["instrument_key"] == instrument_token]
                if not matches.empty:
                    inst_type = str(matches.iloc[0].get("instrument_type", "")).upper()
                    trading_symbol = matches.iloc[0]["symbol"]
                    m = re.match(r"^([A-Z0-9\-&]+?)\d{2}[A-Z]{3}", trading_symbol)
                    base_symbol = m.group(1) if m else trading_symbol
            except Exception as exc:
                log.error("PaperBroker: instrument lookup failed for %s: %s", instrument_token, exc)
        if not base_symbol or not inst_type:
            raise RuntimeError(
                f"PaperBroker: could not resolve instrument details for '{instrument_token}' — "
                f"refusing to place this order rather than guess its margin/balance requirement. "
                f"Try refreshing the instrument master (refresh_instrument_master(force=True))."
            )

        # 2. Query THIS order's own account's available virtual balance —
        # each user has their own paper wallet (see utils/wallet.py,
        # migration 0015). user_id is threaded down from whichever caller
        # actually knows who's trading: RuleBasedStrategy.user_id (custom
        # strategies), the scheduler's strategy.user_id (square-offs), or
        # the authenticated user on a manual terminal trade. Call sites
        # with no real per-request user (the legacy CLI daemon, backtest)
        # leave it None — skip the check rather than guess whose wallet to
        # charge (matches this check's pre-per-user-wallet behavior, which
        # had no user concept to skip in the first place).
        if user_id is None:
            available = float("inf")
        else:
            try:
                summary = get_wallet_summary(user_id)
                available = summary["available_balance"]
            except Exception as exc:
                log.error("PaperBroker: could not query virtual balance for user_id=%s: %s", user_id, exc)
                available = 0.0

        # 3. Perform validations based on segment (Equity vs F&O)
        if inst_type in ("EQUITY", "EQ", "ES"):
            # Spot Equity segment
            if transaction_type.upper() == "BUY":
                cash_required = current_price * quantity
                if cash_required > available:
                    log.error(
                        "PaperBroker REJECTED Equity BUY %s | Qty: %d | Insufficient Balance. Required Cash: %.2f, Available: %.2f",
                        instrument_token, quantity, cash_required, available
                    )
                    raise RuntimeError(
                        f"Insufficient funds: Cash shortfall to buy equity. "
                        f"Required Cash: ₹{cash_required:,.2f}, Available Balance: ₹{available:,.2f}."
                    )
        else:
            # F&O segment (options/futures)
            if transaction_type.upper() == "SELL":
                # Margin requirements for writing options — real
                # Upstox-calculated margin when available, else the
                # flat-rate estimate off a real underlying spot price
                # (never a fabricated fallback — resolve_required_margin
                # raises rather than guess if BOTH are unavailable; the
                # old ₹2000 hardcoded default here was wildly wrong for,
                # say, BANKNIFTY at ~₹57,000, silently under-margining a
                # short by ~28x).
                underlying_spot = None
                try:
                    if hasattr(self.real_broker, "_cache"):
                        underlying_key = self.real_broker._cache.resolve_key(base_symbol)
                        if underlying_key:
                            underlying_spot = self.real_broker.get_ltp(underlying_key)
                except Exception as exc:
                    log.warning("PaperBroker: could not fetch underlying spot for margin check: %s", exc)

                is_index = base_symbol.upper() in INDEX_SYMBOLS
                is_commodity = is_commodity_instrument_key(instrument_token)
                margin_required = resolve_required_margin(
                    self, instrument_token, quantity, "SELL", underlying_spot, is_index, is_commodity, product,
                )

                if margin_required > available:
                    log.error(
                        "PaperBroker REJECTED F&O SELL %s | Qty: %d | Insufficient Balance. Required Margin: %.2f, Available: %.2f",
                        instrument_token, quantity, margin_required, available
                    )
                    raise RuntimeError(
                        f"Margin shortfall: Insufficient funds in virtual wallet. "
                        f"Required Margin: ₹{margin_required:,.2f}, Available Balance: ₹{available:,.2f}."
                    )
            elif transaction_type.upper() == "BUY":
                # Option buy-back cost (debits cash)
                premium_cost = current_price * quantity
                if premium_cost > available:
                    log.error(
                        "PaperBroker REJECTED F&O BUY %s | Qty: %d | Insufficient Balance. Cost: %.2f, Available: %.2f",
                        instrument_token, quantity, premium_cost, available
                    )
                    raise RuntimeError(
                        f"Insufficient funds to close/buy option. "
                        f"Required Cash: ₹{premium_cost:,.2f}, Available Balance: ₹{available:,.2f}."
                    )

        # Slippage always works against us: worse = lower price on a SELL,
        # worse = higher price on a BUY.
        if transaction_type.upper() == "SELL":
            execution_price = current_price * (1.0 - self.slippage_pct)
        else:
            execution_price = current_price * (1.0 + self.slippage_pct)

        order_id = f"PAPER-{uuid.uuid4().hex[:8].upper()}"

        log.info(
            "PaperBroker FILLED %s | %s | Qty: %d | Exec Price: %.2f (Live: %.2f) | ID: %s",
            transaction_type.upper(), instrument_token, quantity, execution_price, current_price, order_id
        )
        return order_id

    def place_sell_order(
        self,
        instrument_token: str,
        quantity: int,
        product: str = "NRML",
        order_type: str = "MARKET",
        tag: str = "",
        user_id: Optional[int] = None,
    ) -> Optional[str]:
        """Place a SELL (write) order for one options leg. See BaseBroker."""
        return self._place_order("SELL", instrument_token, quantity, product, order_type, tag, user_id)

    def place_buy_order(
        self,
        instrument_token: str,
        quantity: int,
        product: str = "NRML",
        order_type: str = "MARKET",
        tag: str = "",
        user_id: Optional[int] = None,
    ) -> Optional[str]:
        """Place a BUY order to square off an options leg. See BaseBroker."""
        return self._place_order("BUY", instrument_token, quantity, product, order_type, tag, user_id)
