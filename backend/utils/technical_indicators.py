"""
utils/technical_indicators.py — pure price-action indicator math (ATR,
Supertrend, standard daily pivot points), used by
strategies/custom/intraday_indicator_strategy.py's Supertrend+Pivot
intraday strategy.

Deliberately pure functions over plain candle dicts — no broker/DB code —
same "independently testable" discipline as utils/option_utils.py and
utils/black76.py. Every function takes candles in CHRONOLOGICAL order
(oldest first) and returns a list of the same length (padded with None
wherever there isn't yet enough history to compute a real value — never a
fabricated number).

Candle shape used throughout: {"high": float, "low": float, "close": float}
(open/volume/timestamp aren't needed by any of the math here).
"""

CandleLike = dict[str, float]


def true_range(candles: list[CandleLike]) -> list[float]:
    """
    True Range per candle — the widest of today's own high-low range and
    the two gaps against yesterday's close (a gap up/down still counts as
    "range travelled," which a plain high-low would miss). The first
    candle has no previous close, so it falls back to its own high-low.
    """
    if not candles:
        return []
    tr = [candles[0]["high"] - candles[0]["low"]]
    for i in range(1, len(candles)):
        high, low = candles[i]["high"], candles[i]["low"]
        prev_close = candles[i - 1]["close"]
        tr.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return tr


def average_true_range(candles: list[CandleLike], period: int) -> list[float | None]:
    """
    Wilder's smoothed ATR — the standard smoothing Supertrend itself is
    defined against (a plain simple-moving-average ATR gives a visibly
    different, non-standard Supertrend line). Returns None for every
    index before `period` candles of history exist (index period-1 is the
    first real value, seeded as the plain average of the first `period`
    true ranges) — never guesses a value from partial data.
    """
    if period < 1:
        raise ValueError(f"ATR period must be >= 1, got {period}.")
    tr = true_range(candles)
    if len(tr) < period:
        return [None] * len(tr)

    atr: list[float | None] = [None] * (period - 1)
    seed = sum(tr[:period]) / period
    atr.append(seed)
    prev = seed
    for i in range(period, len(tr)):
        prev = (prev * (period - 1) + tr[i]) / period
        atr.append(prev)
    return atr


def supertrend(candles: list[CandleLike], period: int = 7, multiplier: float = 3.0) -> list[dict | None]:
    """
    Standard Supertrend indicator (the same "final upper/lower band,
    flip on close crossing the active band" algorithm every charting
    platform implements it as — see e.g. the original Olivier Seban
    definition). Returns one {"value": float, "trend": 1 | -1} dict per
    candle (1 = bullish/uptrend, the Supertrend line sits BELOW price and
    acts as support; -1 = bearish/downtrend, line sits ABOVE price and
    acts as resistance), or None wherever ATR isn't available yet.

    The trend for the FIRST candle with a real ATR value is seeded
    bearish (-1) — an arbitrary but harmless choice standard
    implementations also make (there's no prior candle to derive a real
    flip from); it self-corrects within one candle once price actually
    confirms a side, and this strategy only ever acts on later candles
    once genuine history has accumulated anyway.
    """
    atr = average_true_range(candles, period)
    n = len(candles)
    result: list[dict | None] = [None] * n

    final_upper: list[float | None] = [None] * n
    final_lower: list[float | None] = [None] * n

    first_idx = next((i for i in range(n) if atr[i] is not None), None)
    if first_idx is None:
        return result

    for i in range(first_idx, n):
        hl2 = (candles[i]["high"] + candles[i]["low"]) / 2.0
        basic_upper = hl2 + multiplier * atr[i]
        basic_lower = hl2 - multiplier * atr[i]

        if i == first_idx:
            final_upper[i] = basic_upper
            final_lower[i] = basic_lower
            result[i] = {"value": final_upper[i], "trend": -1}
            continue

        prev_close = candles[i - 1]["close"]
        final_upper[i] = (
            basic_upper if (basic_upper < final_upper[i - 1] or prev_close > final_upper[i - 1]) else final_upper[i - 1]
        )
        final_lower[i] = (
            basic_lower if (basic_lower > final_lower[i - 1] or prev_close < final_lower[i - 1]) else final_lower[i - 1]
        )

        close = candles[i]["close"]
        prev_trend = result[i - 1]["trend"]
        if prev_trend == -1:
            trend = 1 if close > final_upper[i] else -1
        else:
            trend = -1 if close < final_lower[i] else 1

        result[i] = {"value": final_lower[i] if trend == 1 else final_upper[i], "trend": trend}

    return result


def pivot_points(prev_high: float, prev_low: float, prev_close: float) -> dict:
    """
    Standard (classic/floor-trader) daily pivot points, derived from the
    PREVIOUS trading session's high/low/close — the conventional basis
    every "pivot points" indicator on a charting platform uses. Returns
    the full standard set (pp/r1/r2/r3/s1/s2/s3); a strategy only using
    R1/S1 (like this one) just reads those two keys.
    """
    pp = (prev_high + prev_low + prev_close) / 3.0
    r1 = 2 * pp - prev_low
    s1 = 2 * pp - prev_high
    r2 = pp + (prev_high - prev_low)
    s2 = pp - (prev_high - prev_low)
    r3 = prev_high + 2 * (pp - prev_low)
    s3 = prev_low - 2 * (prev_high - pp)
    return {"pp": pp, "r1": r1, "s1": s1, "r2": r2, "s2": s2, "r3": r3, "s3": s3}
