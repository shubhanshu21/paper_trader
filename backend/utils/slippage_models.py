"""
utils/slippage_models.py — pluggable fill-slippage models for MockBroker
and PaperBroker, replacing the single flat `slippage_pct` constant both
previously hardcoded. A model is any `(current_price, quantity, is_sell)
-> slippage_pct` callable; the caller applies it the same way regardless
of which model produced it: `current_price * (1 -+ slippage_pct)`.

A bare float is still accepted everywhere a model is (both brokers wrap it
in fixed_pct() automatically) — nothing that already passes
`slippage_pct=0.001` needs to change.
"""
import random
from typing import Protocol


class SlippageModel(Protocol):
    def __call__(self, current_price: float, quantity: int, is_sell: bool) -> float: ...


def fixed_pct(pct: float) -> SlippageModel:
    """Today's original behavior — the same %% on every fill regardless of size. Default model when a bare float is passed."""
    def _model(current_price: float, quantity: int, is_sell: bool) -> float:
        return pct
    return _model


def volume_scaled_pct(
    base_pct: float,
    quantity_threshold: int,
    scale_per_threshold: float = 0.5,
) -> SlippageModel:
    """
    Slippage grows past `quantity_threshold` — larger F&O orders sweep
    further into the order book than a 1-lot fill, so a 5x-threshold order
    should cost more than a flat %% implies. Below the threshold, behaves
    exactly like fixed_pct(base_pct).

    Args:
        base_pct: slippage fraction for any order at or below the threshold.
        quantity_threshold: order size (in the same units as `quantity` —
            raw contract/share quantity, not lots) above which slippage
            starts scaling up.
        scale_per_threshold: extra fraction of `base_pct` added for every
            complete additional `quantity_threshold` worth of size beyond
            the first. 0.5 (default) means a 3x-threshold order pays
            base_pct * (1 + 0.5 + 0.5) = 2x base_pct.
    """
    def _model(current_price: float, quantity: int, is_sell: bool) -> float:
        if quantity <= quantity_threshold:
            return base_pct
        extra_multiples = (quantity - quantity_threshold) / quantity_threshold
        return base_pct * (1.0 + extra_multiples * scale_per_threshold)
    return _model


def random_band_pct(min_pct: float, max_pct: float, seed: int | None = None) -> SlippageModel:
    """
    Uniform-random slippage within [min_pct, max_pct] per fill, instead of
    a constant — approximates variable market conditions (a calm market
    fills near min_pct, a fast one nearer max_pct) without needing real
    order-book depth data. Deterministic given the same seed, for
    reproducible backtests.
    """
    rng = random.Random(seed)

    def _model(current_price: float, quantity: int, is_sell: bool) -> float:
        return rng.uniform(min_pct, max_pct)
    return _model


def as_model(slippage: "float | SlippageModel") -> SlippageModel:
    """Normalize the `slippage_pct` constructor arg both brokers accept: a bare float becomes fixed_pct(float), a callable passes through unchanged."""
    if isinstance(slippage, (int, float)):
        return fixed_pct(float(slippage))
    return slippage
