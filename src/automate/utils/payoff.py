"""
utils/payoff.py — expiry payoff analysis for a multi-leg options basket.

Pure math, no broker/DB dependency — given each leg's strike/side/quantity
and its CURRENT premium (the real cost/credit if entered right now), this
computes the classic options-strategy payoff diagram: profit or loss as a
function of the underlying's price AT EXPIRY, then reads off the maximum
profit, maximum loss, and breakeven point(s) from that piecewise-linear
function. This is standard options-strategy-builder math (the same thing
Sensibull's payoff chart or any options calculator shows) — not a
probability-weighted forecast, just "if the stock settles at X on expiry
day, this basket is worth Y".
"""
from collections.abc import Callable
from typing import TypedDict


class PayoffLeg(TypedDict):
    strike: float
    option_type: str   # "CE" | "PE"
    action: str         # "BUY" | "SELL"
    quantity: int
    premium: float      # current price of ONE unit (not already multiplied by quantity)


class PayoffResult(TypedDict):
    max_profit: float | None   # None = unbounded (e.g. a naked long call/put)
    max_loss: float | None     # None = unbounded (e.g. a naked short call/put); negative number otherwise
    breakevens: list[float]
    net_premium: float            # positive = net credit collected, negative = net debit paid


def _intrinsic(leg: PayoffLeg, spot: float) -> float:
    if leg["option_type"] == "CE":
        return max(spot - leg["strike"], 0.0)
    return max(leg["strike"] - spot, 0.0)


def _leg_payoff(leg: PayoffLeg, spot: float) -> float:
    intrinsic = _intrinsic(leg, spot)
    if leg["action"] == "BUY":
        return (intrinsic - leg["premium"]) * leg["quantity"]
    return (leg["premium"] - intrinsic) * leg["quantity"]


def total_payoff(legs: list[PayoffLeg], spot: float) -> float:
    return sum(_leg_payoff(leg, spot) for leg in legs)


class PayoffCurvePoint(TypedDict):
    price: float
    pnl: float


def compute_payoff_curve(legs: list[PayoffLeg], spot: float, num_points: int = 41) -> list[PayoffCurvePoint]:
    """
    Sample total_payoff() across a price range around `spot` — the actual
    payoff-DIAGRAM data (P&L vs underlying price at expiry), for charting.
    compute_payoff() above only returns the extremes/breakevens; this is
    everything in between.

    Range is spot * [0.85, 1.15] — wide enough to show the full shape for
    a realistic weekly/monthly expiry move without the chart being mostly
    flat unbounded tails. Every leg's exact strike is included as an extra
    sample point (merged into the evenly-spaced range and de-duplicated)
    so the piecewise-linear function's kinks render exactly at the strike,
    not just approximated by nearby grid points.
    """
    if not legs or spot <= 0:
        return []

    lo, hi = spot * 0.85, spot * 1.15
    step = (hi - lo) / (num_points - 1)
    prices = {round(lo + i * step, 2) for i in range(num_points)}
    for leg in legs:
        if lo <= leg["strike"] <= hi:
            prices.add(round(leg["strike"], 2))

    return [{"price": p, "pnl": round(total_payoff(legs, p), 2)} for p in sorted(prices)]


def compute_payoff(legs: list[PayoffLeg]) -> PayoffResult:
    if not legs:
        return {"max_profit": None, "max_loss": None, "breakevens": [], "net_premium": 0.0}

    net_premium = sum(
        (leg["premium"] * leg["quantity"]) * (1 if leg["action"] == "SELL" else -1)
        for leg in legs
    )

    # The payoff function is piecewise-LINEAR with kinks only at each
    # leg's strike (that's where an intrinsic-value term switches from
    # flat to sloped or vice versa) — so its extremes occur only at
    # spot=0 or at one of those strikes; nowhere else needs checking.
    strikes = sorted({leg["strike"] for leg in legs})
    candidates = [0.0, *strikes]
    values = [(s, total_payoff(legs, s)) for s in candidates]

    # Slope of the payoff line as spot -> +infinity: only CE legs
    # contribute (a PE's intrinsic value flattens to 0 as spot grows).
    # A net long call (BUY CE) position give +quantity of slope (profit
    # grows without bound); a net short call (SELL CE) gives -quantity
    # (loss grows without bound). Spot can't go below 0, so there's no
    # symmetric "-infinity" case to check — the S=0 candidate above
    # already covers that entire boundary.
    slope_at_infinity = sum(
        (leg["quantity"] if leg["action"] == "BUY" else -leg["quantity"])
        for leg in legs if leg["option_type"] == "CE"
    )

    finite_values = [v for _, v in values]
    max_profit = None if slope_at_infinity > 0 else max(finite_values)
    max_loss = None if slope_at_infinity < 0 else min(finite_values)

    # Breakevens: zero-crossings of the piecewise-linear function,
    # scanned segment by segment. A synthetic far-out point is appended
    # so a breakeven that only occurs out on the unbounded ray (e.g. a
    # net debit spread whose breakeven sits beyond the highest strike)
    # is still found by the same linear-interpolation scan, without
    # needing separate handling for the "unbounded" ray.
    scan_points = list(values)
    if slope_at_infinity != 0:
        far_spot = candidates[-1] * 3 + 1000
        far_value = values[-1][1] + slope_at_infinity * (far_spot - candidates[-1])
        scan_points.append((far_spot, far_value))

    breakevens: list[float] = []
    for i in range(len(scan_points) - 1):
        s1, v1 = scan_points[i]
        s2, v2 = scan_points[i + 1]
        if v1 == 0:
            breakevens.append(round(s1, 2))
        elif v1 * v2 < 0:
            t = v1 / (v1 - v2)
            breakevens.append(round(s1 + t * (s2 - s1), 2))
    if scan_points[-1][1] == 0:
        breakevens.append(round(scan_points[-1][0], 2))

    return {
        "max_profit": round(max_profit, 2) if max_profit is not None else None,
        "max_loss": round(max_loss, 2) if max_loss is not None else None,
        "breakevens": sorted(set(breakevens)),
        "net_premium": round(net_premium, 2),
    }


def probability_of_profit(
    legs: list[PayoffLeg], breakevens: list[float], forward: float, iv: float, years_to_expiry: float,
    iv_lookup: Callable[[float], float | None] | None = None,
) -> float | None:
    """
    Market-implied ("risk-neutral") probability this basket is profitable
    at expiry — NOT a real-world forecast, just what the current option
    prices themselves imply, via the same Black-76 lognormal distribution
    every Greek/price in this codebase already uses (see
    utils.black76.prob_underlying_above, i.e. N(d2)).

    Splits the underlying's price axis into intervals at each breakeven,
    determines which intervals are profitable (evaluated at each
    interval's midpoint against the real payoff function), and sums the
    lognormal probability mass of just those intervals.

    `iv_lookup(bound_price)` is an optional per-side volatility override —
    e.g. a strangle's upper tail is really driven by the call leg's own
    (skewed) IV and its lower tail by the put leg's, and blending them into
    one flat ATM number biases the result. When given, it's tried for each
    interval boundary and only falls back to the flat `iv` if it returns
    None/<=0 (e.g. the boundary is 0, which has no meaningful "which leg"
    side, or the broker didn't have a per-leg IV available).

    Returns None if iv/years_to_expiry aren't usable (e.g. IV couldn't be
    solved for any leg) — callers must show "N/A", not a fabricated number.
    """
    if iv is None or iv <= 0 or years_to_expiry is None or years_to_expiry <= 0 or forward <= 0:
        return None

    from automate.utils.black76 import prob_underlying_above

    def resolve_iv(k: float) -> float:
        if iv_lookup is not None:
            override = iv_lookup(k)
            if override is not None and override > 0:
                return override
        return iv

    def prob_above(k: float) -> float:
        if k <= 0:
            return 1.0
        return prob_underlying_above(forward, k, years_to_expiry, resolve_iv(k))

    bounds = [0.0, *sorted({b for b in breakevens if b > 0}), None]  # None = +infinity
    total_prob = 0.0
    for i in range(len(bounds) - 1):
        lo, hi = bounds[i], bounds[i + 1]
        midpoint = (lo + hi) / 2.0 if hi is not None else lo + max(lo, 1.0) * 10  # far out into the profitable/unprofitable tail
        if total_payoff(legs, max(midpoint, 0.01)) <= 0:
            continue
        p_lo = prob_above(lo)
        p_hi = prob_above(hi) if hi is not None else 0.0
        total_prob += max(p_lo - p_hi, 0.0)

    return round(min(max(total_prob, 0.0), 1.0), 4)
