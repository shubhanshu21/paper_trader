"""
api/price_alert_scheduler.py — evaluates every ACTIVE PriceAlert against
real live LTP and fires a notification the moment its condition is met.
Same async background-task pattern as strategy_scheduler.py, registered
in main.py's startup hook.

ABOVE/BELOW fire the first tick the condition is true and stay fired
(never re-check once TRIGGERED). CROSSES_ABOVE/CROSSES_BELOW need the
PREVIOUS tick's price (PriceAlert.last_seen_price) to detect an actual
crossing rather than firing on every tick the price happens to be on one
side — see db.models.PriceAlert's own docstring for why.
"""
import asyncio
from datetime import datetime

from db.engine import SessionLocal
from db.models import PriceAlert
from utils.logger import get_logger
from utils.notify import notify

log = get_logger(__name__)

_TICK_SEC_OPEN = 30
_TICK_SEC_CLOSED = 300


def _market_is_open_now() -> bool:
    from compliance.sebi_rules import assert_market_is_open
    try:
        assert_market_is_open()
        return True
    except RuntimeError:
        return False


def _get_brokers():
    from api.custom_strategy_scheduler import _get_brokers as _shared_get_brokers
    return _shared_get_brokers()


def _condition_met(condition: str, current: float, target: float, last_seen: float | None) -> bool:
    if condition == "ABOVE":
        return current > target
    if condition == "BELOW":
        return current < target
    if condition == "CROSSES_ABOVE":
        return last_seen is not None and last_seen <= target < current
    if condition == "CROSSES_BELOW":
        return last_seen is not None and last_seen >= target > current
    return False


def _run_once() -> None:
    brokers = _get_brokers()
    if brokers is None:
        return
    broker = brokers["paper"]  # read-only LTP lookups only, same rationale every other preview/scan endpoint uses

    db = SessionLocal()
    try:
        alerts = db.query(PriceAlert).filter(PriceAlert.status == "ACTIVE").all()
        if not alerts:
            return

        # One resolve+LTP call per unique symbol, not per alert — several
        # alerts on the same symbol is the common case (e.g. two targets
        # on NIFTY), and a broker call per alert would be pure waste.
        symbols = {a.symbol for a in alerts}
        prices: dict[str, float | None] = {}
        for symbol in symbols:
            try:
                instrument_key = broker.resolve_instrument_key(symbol)
                prices[symbol] = broker.get_ltp(instrument_key)
            except Exception as exc:
                log.warning("price_alert_scheduler: could not fetch LTP for %s: %s", symbol, exc)
                prices[symbol] = None

        for alert in alerts:
            current = prices.get(alert.symbol)
            if current is None:
                continue  # no fresh price this tick — leave last_seen_price untouched, try again next tick rather than guess
            try:
                target = float(alert.target_price)
                last_seen = float(alert.last_seen_price) if alert.last_seen_price is not None else None

                if _condition_met(alert.condition, current, target, last_seen):
                    alert.status = "TRIGGERED"
                    alert.triggered_price = current
                    alert.triggered_at = datetime.now()
                    db.commit()
                    notify(
                        "price_alert",
                        f"🔔 {alert.symbol} {alert.condition.replace('_', ' ').lower()} {target:g} — now {current:g}."
                        + (f" ({alert.note})" if alert.note else ""),
                        level="info", user_id=alert.user_id,
                    )
                    log.info("price_alert_scheduler: alert %s (%s %s %s) triggered at %s", alert.id, alert.symbol, alert.condition, target, current)
                else:
                    alert.last_seen_price = current
                    db.commit()
            except Exception as exc:
                # One alert's failure (a bad DB write, a notify() hiccup)
                # must not abort evaluating every alert queued after it
                # this tick — same discipline as strategy_scheduler.py's
                # per-strategy try/except.
                db.rollback()
                log.error("price_alert_scheduler: alert %s (%s) failed this tick: %s", alert.id, alert.symbol, exc, exc_info=True)
    finally:
        db.close()


async def price_alert_scheduler() -> None:
    """Persistent background task — see module docstring. Never raises; logs and keeps ticking."""
    log.info("price_alert_scheduler: started.")
    while True:
        market_open = _market_is_open_now()
        try:
            if market_open:
                await asyncio.to_thread(_run_once)
        except Exception as exc:
            log.error("price_alert_scheduler: tick failed: %s", exc, exc_info=True)
        await asyncio.sleep(_TICK_SEC_OPEN if market_open else _TICK_SEC_CLOSED)
