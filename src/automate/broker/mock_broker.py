"""
broker/mock_broker.py — Mock Broker for Backtesting

Simulates broker responses using historical data provided by a DataFeed.
Does not make any network calls.
"""
from datetime import datetime
from typing import Optional, List, Dict, Any
import uuid

from automate.broker.base_broker import BaseBroker
from automate.utils.logger import get_logger

log = get_logger(__name__)


class MockBroker(BaseBroker):
    """
    A simulated broker for backtesting strategies against historical data.
    
    It relies on an injected `data_feed` object that holds the state of the 
    market at the current simulated time.
    """

    def __init__(self, data_feed: Any, slippage_pct: float = 0.001) -> None:
        """
        Args:
            data_feed: An object providing `get_ltp(key)`, `get_chain(key, expiry)`, etc.
            slippage_pct: Slippage to apply on order execution (default 0.1%).
        """
        self.data_feed = data_feed
        self.slippage_pct = slippage_pct

        # place_sell_order() always records a (virtual) fill — it never skips
        # the way a live broker's dry-run path does — so this is False. The
        # strategy layer reads broker.dry_run directly to label its result/log
        # as "DRY_RUN" vs "PLACED"/"success".
        self.dry_run = False

        # Virtual portfolio
        self.orders: List[Dict[str, Any]] = []
        self.positions: Dict[str, int] = {}
        
        # In a backtest, we can either use the actual live cache or pass a specific 
        # historical instrument master. For now, we will dynamically fetch it using 
        # the standard InstrumentCache to avoid hardcoding.
        from automate.utils.instrument_cache import InstrumentCache
        self._instrument_cache = InstrumentCache()

    def get_ltp(self, instrument_key: str) -> Optional[float]:
        """Fetch the simulated Last Traded Price from the data feed."""
        ltp = self.data_feed.get_ltp(instrument_key)
        log.debug("MockBroker get_ltp(%s) -> %s", instrument_key, ltp)
        return ltp

    def get_option_contracts(self, instrument_key: str) -> list[str]:
        """Fetch available expiries at the current simulated time."""
        expiries = self.data_feed.get_option_contracts(instrument_key)
        log.debug("MockBroker get_option_contracts(%s) -> %s", instrument_key, expiries)
        return expiries

    def get_option_chain(self, instrument_key: str, expiry_date: str) -> list:
        """Fetch the simulated option chain."""
        chain = self.data_feed.get_option_chain(instrument_key, expiry_date)
        log.debug("MockBroker get_option_chain(%s, %s) -> %d items", instrument_key, expiry_date, len(chain))
        return chain

    def refresh_instrument_master(self, force: bool = False) -> None:
        """No-op for backtesting. Instruments are statically mapped or loaded in data_feed."""
        log.debug("MockBroker refresh_instrument_master() called (No-op).")

    def get_current_time(self) -> Optional[datetime]:
        """Return the backtest's simulated current time (from the data feed)."""
        return self.data_feed.current_time

    def resolve_instrument_key(self, symbol: str) -> str:
        """Resolve dynamically using the standard cache — equity OR index (see InstrumentCache.resolve_key)."""
        key = self._instrument_cache.resolve_key(symbol)
        if not key:
            raise RuntimeError(f"MockBroker: Unknown symbol '{symbol}'")
        return key

    def get_lot_size(self, symbol: str) -> Optional[int]:
        """Real current F&O lot size for `symbol`, from the same cached Upstox instrument master used for resolve_instrument_key(). See BaseBroker."""
        return self._instrument_cache.resolve_lot_size(symbol)

    def get_strike_step(self, symbol: str) -> Optional[float]:
        """Real current strike interval for `symbol`, from the same cached Upstox instrument master. See BaseBroker."""
        return self._instrument_cache.resolve_strike_step(symbol)

    def _place_order(
        self,
        transaction_type: str,
        instrument_token: str,
        quantity: int,
        product: str = "NRML",
        order_type: str = "MARKET",
        tag: str = "",
    ) -> Optional[str]:
        """
        Record a simulated SELL or BUY order. Applies slippage to the
        current LTP.
        """
        current_price = self.get_ltp(instrument_token)
        if current_price is None:
            log.error("MockBroker: Cannot place order for %s, no LTP available.", instrument_token)
            return None

        # Slippage always works against us: worse = lower price on a SELL,
        # worse = higher price on a BUY.
        is_sell = transaction_type.upper() == "SELL"
        execution_price = current_price * (1.0 - self.slippage_pct if is_sell else 1.0 + self.slippage_pct)

        order_id = f"MOCK-{uuid.uuid4().hex[:8].upper()}"

        order = {
            "order_id": order_id,
            "instrument_token": instrument_token,
            "transaction_type": transaction_type.upper(),
            "quantity": quantity,
            "product": product,
            "order_type": order_type,
            "tag": tag,
            "execution_price": execution_price,
            "original_price": current_price,
            "timestamp": self.data_feed.current_time,
            "status": "COMPLETE"
        }

        self.orders.append(order)

        # Update positions
        delta = -quantity if is_sell else quantity
        self.positions[instrument_token] = self.positions.get(instrument_token, 0) + delta

        log.info(
            "MockBroker FILLED %s | %s | Qty: %d | Exec Price: %.2f (Slippage: %.2f) | ID: %s",
            transaction_type.upper(), instrument_token, quantity, execution_price,
            abs(current_price - execution_price), order_id
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
        """Place a SELL (write) order for one options leg. See BaseBroker. user_id unused — backtest has no wallet-balance check."""
        return self._place_order("SELL", instrument_token, quantity, product, order_type, tag)

    def place_buy_order(
        self,
        instrument_token: str,
        quantity: int,
        product: str = "NRML",
        order_type: str = "MARKET",
        tag: str = "",
        user_id: Optional[int] = None,
    ) -> Optional[str]:
        """Place a BUY order to square off an options leg. See BaseBroker. user_id unused — see place_sell_order."""
        return self._place_order("BUY", instrument_token, quantity, product, order_type, tag)
