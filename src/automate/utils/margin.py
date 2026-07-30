"""
utils/margin.py — Rough margin/capital-needed estimate for a short strangle.

Not a real broker SPAN+exposure margin calc — just a flat percentage of
notional, the same simplification backtest/historical_engine.py has always
used (moved here so live-side code — which has no stored spot price — can
share the exact same rates instead of re-deriving its own).
"""

INDEX_SYMBOLS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"}

_INDEX_MARGIN_RATE = 0.11
_STOCK_MARGIN_RATE = 0.18


def estimate_margin_blocked(spot: float, quantity: int, is_index: bool) -> float:
    """Rough estimate of money a broker blocks to sell this strangle. Not your real margin."""
    rate = _INDEX_MARGIN_RATE if is_index else _STOCK_MARGIN_RATE
    return spot * quantity * rate
