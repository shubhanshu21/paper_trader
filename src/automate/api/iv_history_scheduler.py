"""
api/iv_history_scheduler.py — daily ATM-IV snapshot job, once per symbol
per trading day, feeding the entry.condition IV_RANK feature
(rule_schema.py, utils/iv_rank.py). Same asyncio background-task pattern
as token_refresh_scheduler.py/custom_strategy_scheduler.py, registered in
main.py's startup hook — no second systemd service.

Runs shortly after market close (once the day's real closing premiums
have settled) rather than during the session, and only once per calendar
day — checks whether TODAY already has a row for a symbol before writing
another, so a process restart mid-day can't double-write. Symbols
snapshotted: every symbol referenced by any CustomStrategy currently
PAPER_TRADING or LIVE — the only symbols an IV_RANK entry condition could
actually be evaluated against.

This table starts EMPTY — there's no historical options-price archive
live to backfill an IV history from (see db/models.py's SymbolIvHistory
docstring). A symbol's IV rank stays unavailable (utils/iv_rank.py
returns None, entry conditions checking it never trigger) until this job
has been running long enough to accumulate real history — by design, not
a bug to fix later.
"""
import asyncio
import json
from datetime import date, datetime
from datetime import time as dtime
from zoneinfo import ZoneInfo

from automate.db.engine import SessionLocal
from automate.db.models import CustomStrategy, SymbolIvHistory
from automate.strategies.custom.rule_strategy import resolve_leg_strike
from automate.utils import black76
from automate.utils.instrument_cache import InstrumentCache
from automate.utils.logger import get_logger
from automate.utils.option_utils import find_instrument_token, find_nearest_expiry_by_type

log = get_logger(__name__)

_CHECK_INTERVAL_SEC = 300
# A bit after NSE close (15:30 IST) so the day's real closing premiums
# have settled — an IV solved from a mid-session print would be a
# same-day snapshot of "IV right now," not "IV at the close," and would
# make every day's rank comparison apples-to-oranges with the rest.
_SNAPSHOT_AFTER_IST = dtime(15, 35)
_IST = ZoneInfo("Asia/Kolkata")


def _get_brokers():
    from automate.api.custom_strategy_scheduler import _get_brokers as _shared_get_brokers
    return _shared_get_brokers()


def _already_snapshotted_today(db, symbol: str, today: str) -> bool:
    return db.query(SymbolIvHistory).filter(
        SymbolIvHistory.symbol == symbol, SymbolIvHistory.trade_date == today,
    ).first() is not None


def _snapshot_symbol_iv(broker, symbol: str) -> float | None:
    """
    Solve today's ATM IV for `symbol` from its nearest-weekly option
    chain — same Black-76/forward-price pattern
    routes_custom_strategies.py's payoff endpoint already uses (forward
    price from the nearest future, not cash spot — see that endpoint's
    comment on why cash spot biases Black-76). None on any failure
    (broker hiccup, no listed contract, stale/zero premium, etc.) — a
    missing day for a symbol is fine, see utils/iv_rank.py's
    insufficient-history handling; never fabricate a value.
    """
    try:
        instrument_key = broker.resolve_instrument_key(symbol)
        expiries = broker.get_option_contracts(instrument_key)
        if not expiries:
            return None
        now = broker.get_current_time()
        nearest_expiry = find_nearest_expiry_by_type(expiries, "WEEKLY", reference_date=now.date() if now else None)
        if not nearest_expiry:
            return None

        resolved_future = InstrumentCache().resolve_nearest_future_key(symbol)
        forward = broker.get_ltp(resolved_future[0]) if resolved_future else broker.get_ltp(instrument_key)
        if not forward:
            return None

        strike_step = broker.get_strike_step(symbol)
        if not strike_step:
            return None
        atm_strike = resolve_leg_strike(
            {"option_type": "CE", "strike_selection": {"mode": "ATM", "value": None}}, forward, strike_step,
        )

        chain = broker.get_option_chain(instrument_key, nearest_expiry)
        atm_token = find_instrument_token(chain, atm_strike, "CE") if chain else None
        atm_price = broker.get_ltp(atm_token) if atm_token else None
        if not atm_price:
            return None

        days_to_expiry = (date.fromisoformat(nearest_expiry) - date.today()).days
        years_to_expiry = max(days_to_expiry, 1) / 365.0
        return black76.implied_volatility(forward, atm_strike, years_to_expiry, black76.DEFAULT_RISK_FREE_RATE, atm_price, "CE")
    except Exception as exc:
        log.warning("iv_history_scheduler: could not snapshot ATM IV for %s: %s", symbol, exc)
        return None


def _run_daily_snapshot() -> None:
    brokers = _get_brokers()
    if brokers is None:
        return
    # Read-only LTP/chain lookups only — paper broker sees the exact same
    # real market data as live, no reason to need the live broker here.
    broker = brokers["paper"]

    db = SessionLocal()
    try:
        today = date.today().isoformat()
        strategies = db.query(CustomStrategy).filter(CustomStrategy.status.in_(["PAPER_TRADING", "LIVE"])).all()
        symbols = {s for strat in strategies for s in json.loads(strat.symbols)}
        for symbol in symbols:
            if _already_snapshotted_today(db, symbol, today):
                continue
            iv = _snapshot_symbol_iv(broker, symbol)
            if iv is None or iv <= 0:
                continue
            db.add(SymbolIvHistory(symbol=symbol, trade_date=today, atm_iv=round(iv, 4)))
            db.commit()
            log.info("iv_history_scheduler: snapshotted %s ATM IV = %.4f for %s", symbol, iv, today)
    finally:
        db.close()


async def iv_history_scheduler() -> None:
    """Persistent background task — see module docstring. Never raises; logs and keeps ticking."""
    log.info("iv_history_scheduler: started.")
    while True:
        try:
            if datetime.now(_IST).time() >= _SNAPSHOT_AFTER_IST:
                await asyncio.to_thread(_run_daily_snapshot)
        except Exception as exc:
            log.error("iv_history_scheduler: tick-level failure: %s", exc, exc_info=True)
        await asyncio.sleep(_CHECK_INTERVAL_SEC)
