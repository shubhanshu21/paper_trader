"""
utils/margin.py — margin/capital-needed estimate for writing an option.

Two tiers, in priority order:
  1. resolve_required_margin() — the REAL broker-calculated SPAN+exposure
     margin, via BaseBroker.get_required_margin() (Upstox's actual Margin
     Calculator API for UpstoxBroker/PaperBroker; always unavailable for
     MockBroker/backtest, which has no live broker to ask on a historical
     date). This is what live and paper trading now use.
  2. estimate_margin_blocked() — the OLD flat-percentage-of-notional
     fallback, kept for when tier 1 is unavailable (backtest always;
     live/paper only if the Upstox margin API call itself fails, e.g. a
     network hiccup) so sizing/validation never simply breaks.
"""
from typing import Optional

INDEX_SYMBOLS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"}

_INDEX_MARGIN_RATE = 0.11
_STOCK_MARGIN_RATE = 0.18
# MCX SPAN margins run structurally higher than NSE equity F&O (higher
# underlying volatility, no equity-style hedging pool) — conservative flat
# estimate for the fallback tier only; resolve_required_margin() prefers
# the real Upstox-calculated figure whenever it's available.
_COMMODITY_MARGIN_RATE = 0.25


def is_commodity_instrument_key(instrument_key: Optional[str]) -> bool:
    """
    Is this instrument_key an MCX commodity contract? Derived from the
    key's own exchange prefix ('MCX_FO|...') rather than a hardcoded
    symbol list (NSE tickers are user-typed and effectively unbounded,
    but the exchange segment is always right there in the resolved key
    already) — self-maintaining as MCX lists new commodities, unlike
    INDEX_SYMBOLS above (a small, genuinely fixed set NSE itself defines).
    """
    return bool(instrument_key) and instrument_key.startswith("MCX_FO|")


def estimate_margin_blocked(spot: float, quantity: int, is_index: bool, is_commodity: bool = False) -> float:
    """Rough estimate of money a broker blocks to sell this leg. Not your real margin — see module docstring."""
    rate = _COMMODITY_MARGIN_RATE if is_commodity else (_INDEX_MARGIN_RATE if is_index else _STOCK_MARGIN_RATE)
    return spot * quantity * rate


def resolve_required_margin(
    broker, instrument_key: str, quantity: int, transaction_type: str,
    spot: Optional[float], is_index: bool, is_commodity: bool = False, product: str = "D",
) -> float:
    """
    The real broker-calculated margin if `broker` can provide one
    (BaseBroker.get_required_margin — UpstoxBroker/PaperBroker), else the
    flat-rate estimate (needs a real `spot` — pass None if unavailable).
    The one function callers (rule_strategy.py's RISK_PCT sizing,
    paper_broker.py's pre-fill balance check) should use instead of
    calling estimate_margin_blocked() directly, so both stay on the same
    "real margin when we can get it" policy.

    Raises RuntimeError if NEITHER tier is available (no real margin
    figure AND no spot price to estimate from) — refuses rather than
    guessing, same as this codebase already does for lot_size/strike_step.
    """
    try:
        real = broker.get_required_margin(instrument_key, quantity, transaction_type, product)
    except Exception:
        real = None
    if real is not None and real > 0:
        return real
    if spot is None or spot <= 0:
        raise RuntimeError(
            f"Could not determine required margin for {instrument_key} — both the real "
            f"margin API and the underlying spot lookup are unavailable. Refusing to guess."
        )
    return estimate_margin_blocked(spot, quantity, is_index, is_commodity)
