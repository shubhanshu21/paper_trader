"""
api/deps.py — Shared helpers for the control-panel API. The API is an
ADDITIVE read/write interface onto the same files the CLI already uses
(data/runtime/trading.db, data/runtime/strategy_overrides.json,
logs/daemon.pid) — it never duplicates state, and every CLI entry point
keeps working identically whether or not this API process is running.
"""
import logging
from typing import Optional

log = logging.getLogger("api")

_brokers_cache: Optional[dict] = None
_audit_trail = None
_rate_limiter = None


def get_audit_trail():
    global _audit_trail
    if _audit_trail is None:
        from automate.compliance.sebi_rules import AuditTrail
        _audit_trail = AuditTrail(audit_log_path="logs/audit_trail.log")
    return _audit_trail


def get_rate_limiter():
    global _rate_limiter
    if _rate_limiter is None:
        from automate.compliance.sebi_rules import OrderRateLimiter
        _rate_limiter = OrderRateLimiter(max_per_second=10)
    return _rate_limiter


def get_brokers() -> Optional[dict]:
    """
    Lazily build the {'paper': ..., 'live': ...} broker pair (same
    BrokerFactory.create_mode_brokers() the CLI uses), cached for this
    process's lifetime. Returns None (never raises) if Upstox credentials
    are missing/invalid — callers degrade gracefully (e.g. MTM shows
    "price unavailable" instead of crashing the whole positions endpoint).
    """
    global _brokers_cache
    if _brokers_cache is None:
        try:
            from automate.broker.broker_factory import BrokerFactory
            from automate.config import UpstoxConfig

            UpstoxConfig.validate()
            _brokers_cache = BrokerFactory.create_mode_brokers()
        except SystemExit as exc:
            log.warning("Upstox not configured — live prices/manual-close unavailable: %s", exc)
            return None
        except (ValueError, RuntimeError) as exc:
            log.warning("Could not initialise Upstox broker — live prices/manual-close unavailable: %s", exc)
            return None
    return _brokers_cache


def reset_brokers_cache() -> None:
    """Force the next get_brokers() call to reconnect (e.g. after a token refresh)."""
    global _brokers_cache
    _brokers_cache = None


def compute_mtm(position: dict, brokers: Optional[dict]) -> Optional[float]:
    """Gross mark-to-market P&L in rupees — see compute_mtm_economics() for the cost-aware version."""
    econ = compute_mtm_economics(position, brokers)
    return None if econ is None else econ["gross_pnl"]


def compute_mtm_economics(position: dict, brokers: Optional[dict]) -> Optional[dict]:
    """
    gross_pnl/net_pnl/charges for one open short-strangle position, marked
    to market against current LTPs (i.e. "what if I closed it right now"),
    or None if current prices can't be fetched right now. Positive = profit
    (same sign convention as utils.option_utils.strangle_pnl_pct — legs
    have gotten cheaper to buy back since they were sold).
    """
    if brokers is None:
        return None
    broker = brokers.get(position["mode"])
    if broker is None:
        return None
    call_ltp = broker.get_ltp(position["call_token"])
    put_ltp = broker.get_ltp(position["put_token"])
    if call_ltp is None or put_ltp is None:
        return None

    from automate.utils.pnl import compute_strangle_pnl

    return compute_strangle_pnl(
        position["call_entry_price"], position["put_entry_price"],
        call_ltp, put_ltp, position["quantity"],
    )
