"""
broker/upstox_broker.py — Upstox API Wrapper (Market Data + Order Execution).

This module provides a single `UpstoxBroker` class that abstracts all
direct interactions with the Upstox API into clearly named methods.
The rest of the codebase only ever calls this class — never raw SDK calls.

Handles:
  - SDK client initialisation with the daily access_token
  - Fetching real-time LTP (Last Traded Price) for equity spot prices
  - Fetching the option chain and filtering by expiry
  - Placing SELL orders via OrderApiV3 (supports dry-run mode)

Error handling strategy:
  - ApiException (HTTP 4xx/5xx from Upstox) → logged + re-raised as RuntimeError
  - Network/timeout errors → logged + re-raised as RuntimeError
  - Missing data → logged + returns None (callers must validate)
"""

import time
from typing import Optional

import upstox_client
from upstox_client.rest import ApiException

from automate.broker.base_broker import BaseBroker
from automate.utils.instrument_cache import InstrumentCache
from automate.utils.logger import get_logger

log = get_logger(__name__)

# Upstox API version string required by the v2/v3 endpoints
_API_VERSION = "v2"
_ORDER_API_VERSION = "v3"

# Retry settings for transient network errors
_MAX_RETRIES = 3
_RETRY_DELAY_SEC = 2.0


class UpstoxBroker(BaseBroker):
    """
    Production-grade wrapper around the Upstox Python SDK.

    All methods log their inputs/outputs at appropriate levels.
    Sensitive data (access_token) is set once at init and never logged.

    Args:
        access_token: The daily OAuth2 access token (from .env).
        dry_run:      If True, order placements are simulated (logged only).
    """

    def __init__(self, access_token: str, dry_run: bool = True) -> None:
        if not access_token:
            raise ValueError("access_token must not be empty.")

        self.dry_run = dry_run

        # --- Initialise the SDK configuration ---
        # The access_token is injected into the SDK's configuration object.
        # It is never stored as a plain attribute on this class.
        configuration = upstox_client.Configuration()
        configuration.access_token = access_token

        self._api_client = upstox_client.ApiClient(configuration)

        # Instantiate the specific API classes we need
        self._market_quote_v3 = upstox_client.MarketQuoteV3Api(self._api_client)
        # v2 (not v3) — full_market_quote (depth/OHLC/circuit limits) isn't
        # exposed on MarketQuoteV3Api, only the older MarketQuoteApi.
        self._market_quote = upstox_client.MarketQuoteApi(self._api_client)
        self._options_api = upstox_client.OptionsApi(self._api_client)
        self._order_api_v3 = upstox_client.OrderApiV3(self._api_client)
        # place_multi_order() (real batch/basket submission) only exists on
        # the v2 OrderApi, not OrderApiV3 — used by place_basket_sell_order().
        self._order_api_v2 = upstox_client.OrderApi(self._api_client)

        # Instrument master cache (downloaded daily before market open)
        self._cache = InstrumentCache()

        log.info(
            "UpstoxBroker initialised | dry_run=%s",
            self.dry_run,
        )

    # ------------------------------------------------------------------
    # Instrument Master: daily download + symbol resolution
    # ------------------------------------------------------------------

    def refresh_instrument_master(self, force: bool = False) -> None:
        """
        Download (or reload from today's cache) the Upstox NSE instrument master.

        Upstox master CSV columns relevant to us:
          instrument_key   → 'NSE_EQ|INE002A01018'
          tradingsymbol    → 'RELIANCE'
          instrument_type  → 'EQUITY'
          exchange         → 'NSE_EQ'

        Source: https://assets.upstox.com/market-quote/instruments/exchange/NSE.csv
        Run before market open (e.g., 09:00 IST cron) to ensure fresh data.

        Args:
            force: Re-download even if today's cache file already exists.
        """
        log.info("Refreshing Upstox instrument master (force=%s) ...", force)
        self._cache.get_or_refresh(force=force)
        log.info("Upstox instrument master ready.")

    def resolve_instrument_key(self, symbol: str) -> str:
        """
        Dynamically resolve the Upstox NSE_EQ instrument key for a stock symbol.

        Searches the daily instrument master CSV for an exact symbol match
        in the NSE equity segment and returns the instrument_key string.

        Args:
            symbol: NSE trading symbol (e.g., 'RELIANCE', 'TCS').

        Returns:
            Upstox instrument_key string (e.g., 'NSE_EQ|INE002A01018').

        Raises:
            RuntimeError: If the symbol is not found in today's master.
        """
        key = self._cache.resolve_equity_key(symbol)
        if not key:
            raise RuntimeError(
                f"Upstox: Could not resolve instrument_key for '{symbol}'. "
                f"The symbol may be delisted or the instrument master may be stale. "
                f"Try running with force=True: broker.refresh_instrument_master(force=True)"
            )
        return key

    def get_lot_size(self, symbol: str) -> Optional[int]:
        """Real current F&O lot size for `symbol`, from today's instrument master. See BaseBroker."""
        return self._cache.resolve_lot_size(symbol)

    def get_strike_step(self, symbol: str) -> Optional[float]:
        """Real current strike interval for `symbol`, from today's instrument master. See BaseBroker."""
        return self._cache.resolve_strike_step(symbol)

    def get_order_status(self, order_id: str) -> Optional[str]:
        """
        Query the real order status via GET /v2/order/history — an order
        API call returning an order_id does NOT guarantee the exchange
        actually accepted it (e.g. margin shortfall causes a later
        rejection); this is the actual source of truth. See BaseBroker.
        """
        try:
            response = self._order_api_v3.get_order_details(api_version=_ORDER_API_VERSION, order_id=order_id)
        except ApiException as exc:
            log.warning("ApiException fetching order status for '%s': HTTP %s — %s", order_id, exc.status, exc.reason)
            return None
        except Exception as exc:
            log.warning("Unexpected error fetching order status for '%s': %s", order_id, exc)
            return None

        history = response.data or []
        if not history:
            return None
        # History is a list of state transitions for this order_id — the
        # last entry is the most recent/current state.
        return str(history[-1].status).lower()

    # ------------------------------------------------------------------
    # Market Data: Spot Price (LTP)
    # ------------------------------------------------------------------

    def get_ltp(self, instrument_key: str) -> Optional[float]:
        """
        Fetch the Last Traded Price (LTP) for an equity instrument.

        The instrument_key format for NSE equities is:
            'NSE_EQ|<ISIN>'  — e.g., 'NSE_EQ|INE002A01018'  (RELIANCE)

        However, for index/equity LTP, you can also pass the symbol key:
            'NSE_INDEX|Nifty 50'

        For a stock equity: the key is 'NSE_EQ|<instrument_key_from_master>'
        Common keys (from the Upstox instrument master CSV):
            RELIANCE  → 'NSE_EQ|INE002A01018'
            TCS       → 'NSE_EQ|INE467B01029'
            INFY      → 'NSE_EQ|INE009A01021'
            HDFCBANK  → 'NSE_EQ|INE040A01034'

        Args:
            instrument_key: Upstox instrument key for the underlying equity.

        Returns:
            LTP as a float, or None on failure.
        """
        log.info("Fetching LTP for instrument: %s", instrument_key)

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                # The SDK's get_ltp() accepts a comma-separated string of
                # instrument keys and returns a dict keyed by those values.
                response = self._market_quote_v3.get_ltp(
                    instrument_key=instrument_key,
                )

                # The response data is a dict: { "instrument_key": LtpObject }
                quote_map = response.data or {}

                ltp_obj = None
                if len(quote_map) == 1:
                    # Upstox sometimes changes the dict key in the response (e.g., from ISIN to symbol). 
                    # If we only asked for 1 quote, just grab the only value in the dict.
                    ltp_obj = list(quote_map.values())[0]
                else:
                    # Fallback to key lookup if multiple requested (we only ever request 1 here)
                    normalised_key = instrument_key.replace("|", ":")
                    ltp_obj = quote_map.get(normalised_key) or quote_map.get(instrument_key)

                if ltp_obj is None:
                    log.error(
                        "LTP response did not contain data for key '%s'. "
                        "Keys in response: %s",
                        instrument_key,
                        list(quote_map.keys()),
                    )
                    return None

                ltp = float(ltp_obj.last_price)
                log.info("LTP for %s = ₹%.2f", instrument_key, ltp)
                return ltp

            except ApiException as exc:
                log.warning(
                    "ApiException on LTP fetch (attempt %d/%d): HTTP %s — %s",
                    attempt, _MAX_RETRIES, exc.status, exc.reason,
                )
                if attempt < _MAX_RETRIES:
                    time.sleep(_RETRY_DELAY_SEC)
                else:
                    raise RuntimeError(
                        f"Failed to fetch LTP for '{instrument_key}' "
                        f"after {_MAX_RETRIES} attempts."
                    ) from exc

            except Exception as exc:
                log.error("Unexpected error fetching LTP: %s", exc, exc_info=True)
                raise RuntimeError(f"LTP fetch failed: {exc}") from exc

        return None

    def get_ltp_batch(self, instrument_keys: list[str]) -> dict[str, Optional[float]]:
        """
        Fetch LTPs for many instruments in as few HTTP calls as possible
        (Upstox's v3 LTP endpoint accepts a comma-separated instrument_key
        list and returns up to 500 quotes per call) instead of one call per
        instrument.

        Why this exists: market_broadcaster.py polls this every ~2s for
        however many symbols currently have a watchlist subscriber across
        all connected browser tabs. Looping get_ltp() per symbol meant N
        sequential HTTP round-trips per cycle, each with its own up-to-3x
        retry-with-sleep on failure — under a real watchlist's worth of
        symbols this reliably blew past the poll interval and, worse, some
        symbols would intermittently 429/fail while others succeeded,
        which looked like "some rows go live, some stay frozen/grey" in
        the UI for no obvious reason. One batched call removes both the
        volume and the per-symbol flakiness.

        Returns a dict with every requested key present; value is None for
        any instrument Upstox didn't return a quote for.
        """
        results: dict[str, Optional[float]] = {key: None for key in instrument_keys}
        if not instrument_keys:
            return results

        _BATCH_SIZE = 500  # Upstox's own documented cap per call
        for i in range(0, len(instrument_keys), _BATCH_SIZE):
            chunk = instrument_keys[i:i + _BATCH_SIZE]
            joined = ",".join(chunk)
            chunk_set = set(chunk)
            # Response dict keys sometimes use ':' instead of '|', or a
            # symbol-based key instead of the ISIN-based one we sent — match
            # by each quote object's own instrument_token field instead of
            # trusting the response dict's key string, falling back to a
            # ':' <-> '|' swap if instrument_token itself is ever missing.
            colon_to_key = {k.replace("|", ":"): k for k in chunk}

            for attempt in range(1, _MAX_RETRIES + 1):
                try:
                    response = self._market_quote_v3.get_ltp(instrument_key=joined)
                    quote_map = response.data or {}

                    for resp_key, ltp_obj in quote_map.items():
                        matched = getattr(ltp_obj, "instrument_token", None)
                        if matched not in chunk_set:
                            matched = resp_key if resp_key in chunk_set else colon_to_key.get(resp_key)
                        if matched:
                            results[matched] = float(ltp_obj.last_price)
                    break
                except ApiException as exc:
                    log.warning(
                        "ApiException on batch LTP fetch (attempt %d/%d, %d symbols): HTTP %s — %s",
                        attempt, _MAX_RETRIES, len(chunk), exc.status, exc.reason,
                    )
                    if attempt < _MAX_RETRIES:
                        time.sleep(_RETRY_DELAY_SEC)
                except Exception as exc:
                    log.error("Unexpected error fetching batch LTP: %s", exc, exc_info=True)
                    break

        return results

    def get_market_depth(self, instrument_key: str) -> Optional[dict]:
        """
        Fetch the 5-level bid/offer order book plus OHLC, volume, average
        price, and circuit limits for one instrument — everything a market
        depth panel needs, in a single Upstox "full market quote" call.

        Returns None if unavailable (broker error, or your Upstox plan/API
        access doesn't include depth data — callers should degrade to
        showing zeros rather than treating this as fatal, same as the rest
        of this codebase's LTP-unavailable handling).
        """
        try:
            response = self._market_quote.get_full_market_quote(symbol=instrument_key, api_version="2.0")
            quote_map = response.data or {}
        except ApiException as exc:
            log.warning("ApiException on market depth fetch for %s: HTTP %s — %s", instrument_key, exc.status, exc.reason)
            return None
        except Exception as exc:
            log.error("Unexpected error fetching market depth for %s: %s", instrument_key, exc, exc_info=True)
            return None

        quote = None
        for q in quote_map.values():
            if getattr(q, "instrument_token", None) == instrument_key:
                quote = q
                break
        if quote is None and len(quote_map) == 1:
            quote = list(quote_map.values())[0]
        if quote is None:
            return None

        def _level(d) -> dict:
            return {"price": d.price, "qty": d.quantity, "orders": d.orders}

        depth = quote.depth
        buy_levels = [_level(d) for d in (depth.buy if depth else [])][:5]
        sell_levels = [_level(d) for d in (depth.sell if depth else [])][:5]
        while len(buy_levels) < 5:
            buy_levels.append({"price": 0.0, "qty": 0, "orders": 0})
        while len(sell_levels) < 5:
            sell_levels.append({"price": 0.0, "qty": 0, "orders": 0})

        return {
            "instrument_key": instrument_key,
            "last_price": quote.last_price,
            "ohlc": {
                "open": quote.ohlc.open if quote.ohlc else None,
                "high": quote.ohlc.high if quote.ohlc else None,
                "low": quote.ohlc.low if quote.ohlc else None,
                "close": quote.ohlc.close if quote.ohlc else None,  # previous day's close
            },
            "volume": quote.volume,
            "average_price": quote.average_price,
            "lower_circuit_limit": quote.lower_circuit_limit,
            "upper_circuit_limit": quote.upper_circuit_limit,
            "last_trade_time": quote.last_trade_time,
            "total_buy_quantity": quote.total_buy_quantity,
            "total_sell_quantity": quote.total_sell_quantity,
            "buy": buy_levels,
            "sell": sell_levels,
        }

    # ------------------------------------------------------------------
    # Market Data: Option Chain
    # ------------------------------------------------------------------

    def get_option_chain(
        self,
        instrument_key: str,
        expiry_date: str,
    ) -> list:
        """
        Fetch the put-call option chain for an underlying instrument.

        The Upstox OptionsApi.get_put_call_option_chain() returns a list of
        PutCallOptionChainData objects, one per strike price. Each object has
        `.call_options` and `.put_options` attributes containing:
            - instrument_key  (the token used for order placement)
            - strike_price
            - market_data     (greeks, OI, LTP, etc.)

        Instrument key format for NSE F&O underlying:
            'NSE_EQ|<ISIN>'   (same as equity key for single-stock options)

        Expiry date format: 'YYYY-MM-DD'  e.g. '2025-07-31'

        Args:
            instrument_key: Upstox key for the underlying equity/index.
            expiry_date:    Expiry date in 'YYYY-MM-DD' format.

        Returns:
            List of PutCallOptionChainData objects (may be empty on failure).
        """
        log.info(
            "Fetching option chain for %s | expiry: %s",
            instrument_key,
            expiry_date,
        )

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                response = self._options_api.get_put_call_option_chain(
                    instrument_key=instrument_key,
                    expiry_date=expiry_date,
                )

                chain_data = response.data or []
                log.info(
                    "Option chain fetched: %d strike entries for expiry %s.",
                    len(chain_data),
                    expiry_date,
                )
                return chain_data

            except ApiException as exc:
                log.warning(
                    "ApiException on option chain fetch (attempt %d/%d): "
                    "HTTP %s — %s",
                    attempt, _MAX_RETRIES, exc.status, exc.reason,
                )
                if attempt < _MAX_RETRIES:
                    time.sleep(_RETRY_DELAY_SEC)
                else:
                    raise RuntimeError(
                        f"Failed to fetch option chain for '{instrument_key}' "
                        f"expiry {expiry_date} after {_MAX_RETRIES} attempts."
                    ) from exc

            except Exception as exc:
                log.error("Unexpected error fetching option chain: %s", exc, exc_info=True)
                raise RuntimeError(f"Option chain fetch failed: {exc}") from exc

        return []

    # ------------------------------------------------------------------
    # Market Data: Available Option Expiries
    # ------------------------------------------------------------------

    def get_option_contracts(self, instrument_key: str) -> list[str]:
        """
        Retrieve all available option expiry dates for an underlying.

        The OptionsApi.get_option_contracts() returns a list of
        OptionContractData objects. We extract the unique expiry dates
        so the strategy can pick the nearest monthly expiry.

        Args:
            instrument_key: Upstox key for the underlying equity.

        Returns:
            Sorted list of expiry date strings in 'YYYY-MM-DD' format.
        """
        log.info("Fetching available expiries for %s ...", instrument_key)

        try:
            response = self._options_api.get_option_contracts(
                instrument_key=instrument_key,
            )
            contracts = response.data or []

            # Extract unique expiry dates from the contracts list
            # Upstox SDK might return datetime objects or strings depending on version.
            expiry_strs = set()
            for c in contracts:
                if hasattr(c, "expiry") and c.expiry:
                    if isinstance(c.expiry, str):
                        # Some versions return string with time, e.g. '2025-07-31 00:00:00'
                        expiry_strs.add(c.expiry.split(" ")[0])
                    else:
                        # Assuming datetime object
                        expiry_strs.add(c.expiry.strftime("%Y-%m-%d"))

            expiries = sorted(expiry_strs)
            log.info("Found %d unique expiry dates for %s.", len(expiries), instrument_key)
            return expiries

        except ApiException as exc:
            log.error(
                "ApiException fetching option contracts: HTTP %s — %s",
                exc.status, exc.reason,
            )
            raise RuntimeError(
                f"Could not fetch option contracts for '{instrument_key}'."
            ) from exc

        except Exception as exc:
            log.error("Unexpected error: %s", exc, exc_info=True)
            raise RuntimeError(f"Option contracts fetch failed: {exc}") from exc

    # ------------------------------------------------------------------
    # Order Execution: Place SELL / BUY Order
    # ------------------------------------------------------------------

    def _place_order(
        self,
        transaction_type: str,
        instrument_token: str,
        quantity: int,
        product: str = "D",
        order_type: str = "MARKET",
        tag: str = "",
    ) -> Optional[str]:
        """
        Place a SELL or BUY order for one options leg via the v3 OrderApi.

        The PlaceOrderV3Request payload explained:
          - quantity:           Total units (lot_size × num_lots)
          - product:            'D' = NRML (overnight), 'I' = MIS (intraday)
          - validity:           'DAY' = valid for the current session only
          - price:              0 for MARKET orders (ignored by exchange)
          - tag:                Optional 16-char label shown in order book
          - instrument_token:   Upstox token e.g. 'NSE_FO|12345'
          - order_type:         'MARKET' or 'LIMIT'
          - transaction_type:   'SELL' (write to open) or 'BUY' (square off)
          - disclosed_quantity: 0 = do not disclose partial quantity
          - trigger_price:      0 for non-SL orders
          - is_amo:             False = regular session, True = after-market
          - slice:              True = auto-slice if qty > freeze limit

        Args:
            transaction_type: 'SELL' or 'BUY'.
            instrument_token: Upstox instrument token for the option contract.
            quantity:         Number of units (lot_size × lots).
            product:          'D' for NRML or 'I' for MIS.
            order_type:       'MARKET' (default) or 'LIMIT'.
            tag:              Order tag for identification (max 16 chars).

        Returns:
            Order ID string if placed, or None on dry-run / failure.
        """
        # Sanitise tag length (Upstox limit = 16 characters)
        tag = (tag[:16] if len(tag) > 16 else tag)

        order_request = upstox_client.PlaceOrderV3Request(
            quantity=quantity,
            product=product,           # 'D' = NRML | 'I' = MIS
            validity="DAY",            # Always DAY for options swing entries
            price=0,                   # 0 = market order (price ignored)
            tag=tag,
            instrument_token=instrument_token,
            order_type=order_type,     # 'MARKET' or 'LIMIT'
            transaction_type=transaction_type,
            disclosed_quantity=0,      # No partial disclosure
            trigger_price=0,           # Not a stop-loss order
            is_amo=False,              # Regular market hours, not AMO
            slice=True,                # Auto-slice for large qty orders
        )

        # --- DRY RUN: log the order and return without sending ---
        if self.dry_run:
            log.warning(
                "[DRY RUN] Would %s | token=%s | qty=%d | product=%s | "
                "order_type=%s | tag=%s",
                transaction_type, instrument_token, quantity, product, order_type, tag,
            )
            return None

        # --- LIVE: Send the order to Upstox ---
        log.info(
            "Placing LIVE %s order | token=%s | qty=%d | product=%s",
            transaction_type, instrument_token, quantity, product,
        )

        try:
            response = self._order_api_v3.place_order(
                body=order_request,
                api_version=_ORDER_API_VERSION,
            )
            order_id = response.data.order_id if response.data else "UNKNOWN"
            log.info(
                "Order placed successfully | order_id=%s | token=%s",
                order_id, instrument_token,
            )
            return order_id

        except ApiException as exc:
            log.error(
                "ApiException placing order for token '%s': HTTP %s — %s",
                instrument_token, exc.status, exc.body,
            )
            raise RuntimeError(
                f"Order placement failed for token '{instrument_token}': "
                f"HTTP {exc.status}"
            ) from exc

        except Exception as exc:
            log.error(
                "Unexpected error placing order for token '%s': %s",
                instrument_token, exc, exc_info=True,
            )
            raise RuntimeError(f"Order placement failed: {exc}") from exc

    def place_sell_order(
        self,
        instrument_token: str,
        quantity: int,
        product: str = "D",
        order_type: str = "MARKET",
        tag: str = "",
        user_id: Optional[int] = None,
    ) -> Optional[str]:
        """Place a SELL (write) order for one options leg. See BaseBroker. user_id unused — a live order is checked against the real broker's own real margin, not a simulated wallet."""
        return self._place_order("SELL", instrument_token, quantity, product, order_type, tag)

    def place_buy_order(
        self,
        instrument_token: str,
        quantity: int,
        product: str = "D",
        order_type: str = "MARKET",
        tag: str = "",
        user_id: Optional[int] = None,
    ) -> Optional[str]:
        """Place a BUY order to square off an options leg. See BaseBroker. user_id unused — see place_sell_order."""
        return self._place_order("BUY", instrument_token, quantity, product, order_type, tag)

    # ------------------------------------------------------------------
    # Order Execution: Basket (multi-order) SELL
    # ------------------------------------------------------------------

    def place_basket_sell_order(self, legs: list[dict]) -> dict:
        """
        Submit all legs in a single Upstox multi-order batch call
        (POST /v2/order/multi/place, via the v2 OrderApi — this endpoint
        doesn't exist on OrderApiV3) instead of separate sequential
        requests, reducing the latency window between legs.

        NOT atomic: Upstox's own MultiOrderResponse.status can be
        "partial_success" — some legs can fill while others fail even
        within this single batch call. See BaseBroker.place_basket_sell_order
        for the contract callers must honor.
        """
        if self.dry_run:
            results: dict = {}
            for leg in legs:
                tag = leg.get("tag", "")
                tag = tag[:16] if len(tag) > 16 else tag
                log.warning(
                    "[DRY RUN] Would SELL (basket) | token=%s | qty=%d | "
                    "product=%s | tag=%s",
                    leg["instrument_token"], leg["quantity"],
                    leg.get("product", "D"), tag,
                )
                results[leg["correlation_id"]] = None
            return results

        body = []
        for leg in legs:
            tag = leg.get("tag", "")
            tag = tag[:16] if len(tag) > 16 else tag
            body.append(upstox_client.MultiOrderRequest(
                quantity=leg["quantity"],
                product=leg.get("product", "D"),
                validity="DAY",
                price=0,
                tag=tag,
                slice=True,
                instrument_token=leg["instrument_token"],
                order_type=leg.get("order_type", "MARKET"),
                transaction_type="SELL",
                disclosed_quantity=0,
                trigger_price=0,
                is_amo=False,
                correlation_id=leg["correlation_id"],
            ))

        log.info("Placing LIVE multi-order SELL batch | %d legs", len(body))

        try:
            response = self._order_api_v2.place_multi_order(body=body)
        except ApiException as exc:
            log.error(
                "ApiException placing multi-order batch: HTTP %s — %s",
                exc.status, exc.body,
            )
            raise RuntimeError(
                f"Multi-order batch placement failed: HTTP {exc.status}"
            ) from exc
        except Exception as exc:
            log.error("Unexpected error placing multi-order batch: %s", exc, exc_info=True)
            raise RuntimeError(f"Multi-order batch placement failed: {exc}") from exc

        results = {leg["correlation_id"]: None for leg in legs}
        for item in (response.data or []):
            results[item.correlation_id] = item.order_id
        for err in (response.errors or []):
            log.error(
                "Multi-order leg FAILED | correlation_id=%s | %s: %s",
                err.correlation_id, err.error_code, err.message,
            )

        log.info(
            "Multi-order batch result: status=%s | %d/%d legs filled",
            response.status, sum(1 for v in results.values() if v), len(legs),
        )
        return results
