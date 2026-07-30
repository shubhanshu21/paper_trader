"""
broker/base_broker.py — Abstract Broker Interface.

Defines the contract every broker implementation MUST fulfill.
The strategy layer only ever calls methods defined here, making
brokers fully interchangeable without touching strategy code.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional


class BaseBroker(ABC):
    """
    Abstract interface for all broker integrations.

    BaseBroker — the interface UpstoxBroker/PaperBroker/MockBroker implement:
      - get_ltp()             → fetch real-time spot price
      - get_option_contracts()→ list all available expiry dates
      - get_option_chain()    → fetch strikes + instrument tokens for an expiry
      - place_sell_order()    → place a SELL MARKET order for one options leg
    """

    @abstractmethod
    def get_ltp(self, instrument_key: str) -> Optional[float]:
        """
        Fetch the Last Traded Price of an equity underlying.

        Args:
            instrument_key: Broker-specific identifier for the stock.

        Returns:
            LTP as float, or None on failure.
        """
        ...

    def get_ltp_batch(self, instrument_keys: list[str]) -> dict[str, Optional[float]]:
        """
        Fetch LTPs for many instruments at once. Default implementation is
        just N calls to get_ltp() — correct for any broker, but callers that
        poll many symbols on a tight interval (e.g. api/market_broadcaster.py)
        should prefer a broker that overrides this with a real batched call
        (see UpstoxBroker.get_ltp_batch) to avoid N sequential round-trips
        and the per-symbol rate-limit flakiness that comes with them. Not
        abstract, so existing broker implementations don't need to change.
        """
        return {key: self.get_ltp(key) for key in instrument_keys}

    def get_market_depth(self, instrument_key: str) -> Optional[dict]:
        """
        5-level bid/offer book + OHLC/volume/circuit-limit snapshot for one
        instrument. Not every broker implementation can provide this (paper
        trading has no real order book) — default is None, meaning "no depth
        data available," which callers must treat as a normal, expected
        outcome (show zeros), not an error. See UpstoxBroker.get_market_depth
        for the real implementation.
        """
        return None

    @abstractmethod
    def get_option_contracts(self, instrument_key: str) -> list[str]:
        """
        Return all available option expiry dates for an underlying.

        Args:
            instrument_key: Broker-specific identifier.

        Returns:
            Sorted list of expiry strings ('YYYY-MM-DD').
        """
        ...

    @abstractmethod
    def get_option_chain(self, instrument_key: str, expiry_date: str) -> list:
        """
        Fetch full option chain data for an underlying + expiry.

        Args:
            instrument_key: Broker-specific identifier.
            expiry_date:    Expiry in 'YYYY-MM-DD' format.

        Returns:
            List of chain entries (structure varies per broker;
            consumed by broker-specific token finders).
        """
        ...

    @abstractmethod
    def refresh_instrument_master(self, force: bool = False) -> None:
        """
        Download (or reload from cache) today's instrument master.

        Must be called once before the trading session begins — typically
        at 09:00 IST via cron, before the market opens at 09:15 IST.

        Args:
            force: If True, re-download even if a fresh cache exists today.
        """
        ...

    @abstractmethod
    def resolve_instrument_key(self, symbol: str) -> str:
        """
        Dynamically resolve the broker-specific identifier for an NSE equity.

        Replaces all hardcoded symbol→token maps. Uses the daily instrument
        master to find the correct key at runtime.

        Args:
            symbol: NSE trading symbol (e.g., 'RELIANCE', 'TCS').

        Returns:
            Broker-specific instrument key/token/security_id string.

        Raises:
            RuntimeError: If the symbol is not found in the instrument master.
        """
        ...

    @abstractmethod
    def place_sell_order(
        self,
        instrument_token: str,
        quantity: int,
        product: str = "NRML",
        order_type: str = "MARKET",
        tag: str = "",
    ) -> Optional[str]:
        """
        Place a SELL order for an options leg.

        Args:
            instrument_token: Broker-specific token / trading symbol.
            quantity:         Total units (lot_size × lots).
            product:          'NRML' for overnight, 'MIS' for intraday.
            order_type:       'MARKET' or 'LIMIT'.
            tag:              Order tag / correlation ID.

        Returns:
            Order ID string on success, None on dry-run.
        """
        ...

    @abstractmethod
    def place_buy_order(
        self,
        instrument_token: str,
        quantity: int,
        product: str = "NRML",
        order_type: str = "MARKET",
        tag: str = "",
    ) -> Optional[str]:
        """
        Place a BUY order for an options leg.

        Used to square off (unwind) a leg that already sold successfully
        when a companion leg in the same basket fails to fill — see the
        calling strategy's partial-fill handling. Never used for opening a
        new long position; this codebase only ever sells to open.

        Args:
            instrument_token: Broker-specific token / trading symbol.
            quantity:         Total units (lot_size × lots).
            product:          'NRML' for overnight, 'MIS' for intraday.
            order_type:       'MARKET' or 'LIMIT'.
            tag:              Order tag / correlation ID.

        Returns:
            Order ID string on success, None on dry-run.
        """
        ...

    def place_basket_sell_order(self, legs: list[dict]) -> dict:
        """
        Attempt to place several SELL orders together.

        Default implementation places them sequentially via
        `place_sell_order()`, one per leg. UpstoxBroker overrides this with
        a real single-call batch API (`POST /v2/order/multi/place`).

        NOT atomic on ANY broker, including Upstox — its own
        MultiOrderResponse.status can be "partial_success". Callers MUST
        handle partial fills (some legs filled, others None) themselves —
        see the calling strategy's auto-unwind logic.

        Args:
            legs: List of dicts, each with keys:
                correlation_id  — caller-chosen label used to map results
                                  back to legs (e.g. "CE"/"PE").
                instrument_token, quantity, product, order_type, tag
                                — same meaning as place_sell_order()'s args.

        Returns:
            Dict of correlation_id -> order_id (None if that leg failed or
            the broker is in dry-run mode).
        """
        results: dict = {}
        for leg in legs:
            results[leg["correlation_id"]] = self.place_sell_order(
                instrument_token=leg["instrument_token"],
                quantity=leg["quantity"],
                product=leg.get("product", "NRML"),
                order_type=leg.get("order_type", "MARKET"),
                tag=leg.get("tag", ""),
            )
        return results

    def get_current_time(self) -> Optional[datetime]:
        """
        Return the timestamp the SEBI market-hours check should validate
        against, or None to use the real wall-clock time.

        Live and paper brokers use the default (None → real time). Simulated
        brokers (e.g. MockBroker in backtesting) override this to return the
        backtest's simulated current time, so the market-hours/weekend/holiday
        gate is evaluated against the historical date being simulated instead
        of whenever the backtest happens to actually run.
        """
        return None

    def get_lot_size(self, symbol: str) -> Optional[int]:
        """
        Return the REAL current F&O lot size for `symbol`, resolved from
        this broker's live instrument master, or None if this broker
        doesn't support dynamic resolution.

        Default (None) means "not implemented for this broker" — callers
        must refuse to trade rather than guess/default to 1 (there is no
        hardcoded fallback table anywhere in this codebase; see
        TenPercentOTMStrangle.__init__()). MockBroker and UpstoxBroker
        override this today (MockBroker delegates to the same cached
        Upstox instrument master); NSE revises lot sizes periodically, so
        dynamic resolution from a daily-refreshed source is strictly more
        trustworthy than any static table would be.
        """
        return None

    def get_strike_step(self, symbol: str) -> Optional[float]:
        """
        Return the REAL current strike-price interval for `symbol`,
        resolved from this broker's live instrument master (the actual
        gap between listed option strikes for the nearest expiry), or
        None if this broker doesn't support dynamic resolution.

        Same reasoning and same "no hardcoded fallback table, refuse to
        trade rather than guess" contract as get_lot_size() — verified
        live that a hardcoded strike-step table drifts out of date the
        same way a hardcoded lot-size table did (e.g. RELIANCE configured
        as 20, really 10; TCS configured as 50, really 20). Can be
        fractional for lower-priced stocks (e.g. 2.5), hence float not int.
        """
        return None

    def get_order_status(self, order_id: str) -> Optional[str]:
        """
        Return the current status of a previously-placed order (e.g.
        'complete', 'rejected', 'cancelled', 'open'), lowercased, or None
        if this broker doesn't support status lookup or the order isn't found.

        Used for post-order reconciliation: an order API call can return
        an order_id and still have the exchange reject it afterward (e.g.
        insufficient margin) — a returned order_id alone does NOT guarantee
        the leg actually filled. Only UpstoxBroker overrides this today.
        """
        return None
