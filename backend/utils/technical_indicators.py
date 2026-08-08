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


def camarilla_pivot_points(prev_high: float, prev_low: float, prev_close: float) -> dict:
    """
    Camarilla pivot points — a DIFFERENT formula from the classic
    pivot_points() above (both take the same prev_high/prev_low/prev_close
    inputs, but R3/S3 mean different price levels under each). Used by the
    Gravity strategy's (strategies/custom/gravity_schema.py) fakeout-
    reversal signal, which trades price breaching R3/S3 and closing back
    inside — Camarilla's R3/S3 sit noticeably tighter around `prev_close`
    than the classic formula's, which is the whole point of using this
    variant for a mean-reversion/fakeout read rather than a breakout one.
    Only r3/s3 are used by that strategy; r1/r2/s1/s2 are returned for
    completeness (the standard Camarilla set), same convention as
    pivot_points() above.
    """
    rng = prev_high - prev_low
    r1 = prev_close + rng * 1.1 / 12
    r2 = prev_close + rng * 1.1 / 6
    r3 = prev_close + rng * 1.1 / 4
    r4 = prev_close + rng * 1.1 / 2
    s1 = prev_close - rng * 1.1 / 12
    s2 = prev_close - rng * 1.1 / 6
    s3 = prev_close - rng * 1.1 / 4
    s4 = prev_close - rng * 1.1 / 2
    return {"r1": r1, "r2": r2, "r3": r3, "r4": r4, "s1": s1, "s2": s2, "s3": s3, "s4": s4}


def ema(values: list[float], period: int) -> list[float | None]:
    """
    Standard exponential moving average, seeded with a plain SMA of the
    first `period` values (the conventional way every charting platform
    seeds an EMA) — None for every index before that seed exists.
    """
    if period < 1:
        raise ValueError(f"EMA period must be >= 1, got {period}.")
    if len(values) < period:
        return [None] * len(values)
    k = 2.0 / (period + 1)
    out: list[float | None] = [None] * (period - 1)
    seed = sum(values[:period]) / period
    out.append(seed)
    prev = seed
    for v in values[period:]:
        prev = v * k + prev * (1 - k)
        out.append(prev)
    return out


def macd(candles: list[CandleLike], fast: int = 12, slow: int = 26, signal: int = 9) -> list[dict | None]:
    """
    Standard MACD (fast EMA - slow EMA, with a signal-line EMA of that
    difference) — used by the MACD credit-spread strategy's stop-and-
    reverse trend read (strategies/custom/macd_credit_schema.py). Returns
    one {"macd": float, "signal": float, "histogram": float} dict per
    candle, or None wherever the slow EMA (or, after that, the signal
    EMA of the MACD line itself) doesn't have enough history yet —
    signal only becomes available `signal` candles after the MACD line
    itself first does, so the None-padding is deeper than just `slow`.
    """
    closes = [c["close"] for c in candles]
    fast_ema = ema(closes, fast)
    slow_ema = ema(closes, slow)
    macd_line = [None if (f is None or s is None) else f - s for f, s in zip(fast_ema, slow_ema, strict=True)]

    first_valid = next((i for i, v in enumerate(macd_line) if v is not None), None)
    if first_valid is None:
        return [None] * len(candles)
    macd_values = [v for v in macd_line[first_valid:] if v is not None]
    signal_ema = ema(macd_values, signal)

    result: list[dict | None] = [None] * first_valid
    for i, s in enumerate(signal_ema):
        m = macd_values[i]
        result.append(None if s is None else {"macd": m, "signal": s, "histogram": m - s})
    return result


def rsi(values: list[float], period: int = 14) -> list[float | None]:
    """
    Wilder's RSI (0-100) — same Wilder-smoothing family as
    average_true_range() above, seeded with a plain average of the first
    `period` gains/losses then smoothed forward. None for every index
    before that seed exists. A period with zero average loss returns 100
    (fully overbought, no divide-by-zero) rather than raising.
    """
    if period < 1:
        raise ValueError(f"RSI period must be >= 1, got {period}.")
    if len(values) < period + 1:
        return [None] * len(values)

    deltas = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    out: list[float | None] = [None] * period  # values[0..period-1] have no RSI yet

    def _rsi_from_averages(gain: float, loss: float) -> float:
        if loss == 0:
            return 100.0
        rs = gain / loss
        return 100.0 - (100.0 / (1.0 + rs))

    out.append(_rsi_from_averages(avg_gain, avg_loss))
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out.append(_rsi_from_averages(avg_gain, avg_loss))
    return out


def bollinger_bands(values: list[float], period: int = 20, num_std: float = 2.0) -> list[dict | None]:
    """
    Standard Bollinger Bands — SMA(period) midline, upper/lower bands
    `num_std` population-standard-deviations away. Also returns `width`
    ((upper - lower) / middle, the normalized band-width used for
    volatility-squeeze entry conditions — a low `width` means the market
    has gone quiet, historically often the run-up to a breakout). None for
    every index before `period` closes exist.
    """
    if period < 2:
        raise ValueError(f"Bollinger period must be >= 2, got {period}.")
    if len(values) < period:
        return [None] * len(values)

    out: list[dict | None] = [None] * (period - 1)
    for i in range(period - 1, len(values)):
        window = values[i - period + 1: i + 1]
        middle = sum(window) / period
        variance = sum((v - middle) ** 2 for v in window) / period
        stdev = variance ** 0.5
        upper = middle + num_std * stdev
        lower = middle - num_std * stdev
        width = (upper - lower) / middle if middle != 0 else None
        out.append({"middle": middle, "upper": upper, "lower": lower, "width": width})
    return out
