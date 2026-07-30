"""
api/market_broadcaster.py — Background task that turns /ws/market from a
subscription registry with no producer into an actually-live feed.

websocket_manager.ConnectionManager already handles connect/subscribe/
broadcast plumbing; nothing in the codebase was calling broadcast_to_symbol()
for real prices. This task polls the broker for whatever instrument_keys
currently have at least one subscriber and fans out ticks to them, so the
server does the polling once per interval no matter how many browser tabs
are watching a given symbol.
"""
import asyncio
import logging
import time

from automate.api.deps import get_brokers
from automate.api.websocket_manager import manager

log = logging.getLogger("api.market_broadcaster")

_POLL_INTERVAL_SEC = 2.0


def _fetch_ticks(symbols: list[str]) -> dict[str, float]:
    """
    Blocking broker call — run via asyncio.to_thread from the event loop.
    Uses get_ltp_batch() (one HTTP call for up to 500 symbols, per Upstox's
    own documented limit) instead of one call per symbol: looping get_ltp()
    per instrument every 2s was both slow for a real watchlist's worth of
    symbols and prone to intermittent per-symbol rate-limit failures under
    load — some rows would silently stop updating while others kept working.
    """
    brokers = get_brokers()
    if not brokers:
        return {}
    broker = brokers.get("paper")  # PaperBroker.get_ltp_batch delegates to the live Upstox quote API
    if broker is None:
        return {}

    try:
        results = broker.get_ltp_batch(symbols)
    except Exception as exc:
        log.debug("market_broadcaster: batch LTP fetch failed: %s", exc)
        return {}
    return {key: ltp for key, ltp in results.items() if ltp is not None}


async def market_price_broadcaster():
    """Runs for the lifetime of the API process (started on app startup)."""
    log.info("Market price broadcaster started (poll interval=%.1fs).", _POLL_INTERVAL_SEC)
    while True:
        try:
            symbols = list(manager.symbol_subscriptions.keys())
            if symbols:
                ticks = await asyncio.to_thread(_fetch_ticks, symbols)
                ts = time.time()
                for key, ltp in ticks.items():
                    await manager.broadcast_to_symbol(
                        key, {"type": "tick", "instrument_key": key, "ltp": ltp, "ts": ts}
                    )
        except Exception:
            log.exception("market_price_broadcaster loop error")
        await asyncio.sleep(_POLL_INTERVAL_SEC)
