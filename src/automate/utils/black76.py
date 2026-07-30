"""
utils/black76.py — Black-76 option pricing, implied volatility, and Greeks.

This is the same model Indian retail platforms (Sensibull's calculator,
Zerodha's option calculator, Rupeezy) use for NSE F&O options, in place of
plain Black-Scholes: NSE index/stock options are effectively struck
against the FUTURES price, not raw spot, so Black-76 substitutes the
futures price F for spot S — this already bakes in cost-of-carry/dividend
effects that Black-Scholes would otherwise need adjusted for separately.

No third-party pricing library (scipy isn't a dependency here) — the
normal CDF/PDF are implemented directly via math.erf, which is exact
(not an approximation), so there's no accuracy tradeoff from skipping
scipy.

Pipeline for turning a real traded option price into Greeks (matches how
every broker above actually does it — volatility is never an input
that's just "known", it's solved for):

    1. implied_volatility(F, K, T, r, market_price, option_type)
       — invert the pricing formula against the option's real traded
       price (Newton-Raphson, falling back to bisection if it diverges).
    2. greeks(F, K, T, r, sigma, option_type)
       — plug that solved sigma back into the closed-form Greek formulas.

compute_greeks_from_market_price() does both steps in one call — this is
the function most callers actually want.
"""
import math
from typing import Literal, Optional, TypedDict

OptionType = Literal["CE", "PE"]

# Indian F&O options are European-style (cash/physically settled only at
# expiry, no early exercise) — Black-76 assumes exactly this, so no early
# -exercise premium is being ignored here.

_SQRT_2 = math.sqrt(2.0)
_SQRT_2PI = math.sqrt(2.0 * math.pi)

# Approximate 91-day T-bill rate — same "not live-fetched, periodically
# updated" convention every Indian retail Greeks calculator (Sensibull,
# Zerodha) uses; Greeks are not sensitive enough to small rate changes
# for this to matter for a retail strategy's decision-making. Update this
# constant occasionally rather than wiring a live rate feed for it.
DEFAULT_RISK_FREE_RATE = 0.065


def _norm_cdf(x: float) -> float:
    """Standard normal CDF — exact via math.erf, not a polynomial approximation."""
    return 0.5 * (1.0 + math.erf(x / _SQRT_2))


# Public alias — utils/payoff.py's probability-of-profit calculation reuses
# this exact CDF (and the SAME d1/d2 Black-76 gives every Greek/price from)
# rather than a second implementation, so "N(d2)" means the same thing
# everywhere in this codebase.
norm_cdf = _norm_cdf


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / _SQRT_2PI


def _d1_d2(F: float, K: float, T: float, sigma: float) -> tuple[float, float]:
    if F <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        raise ValueError(f"F, K, T, sigma must all be positive (got F={F}, K={K}, T={T}, sigma={sigma}).")
    sqrt_t = math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * T) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    return d1, d2


def prob_underlying_above(F: float, K: float, T: float, sigma: float) -> float:
    """
    Risk-neutral probability (under this same Black-76 model — i.e. the
    market-implied probability, not a real-world forecast) that the
    underlying settles ABOVE K at expiry — this is exactly N(d2), the
    same term every call/put price above is already built from. Used by
    utils/payoff.py for "Probability of Profit". Use
    `1 - prob_underlying_above(...)` for "below K".
    """
    _, d2 = _d1_d2(F, K, T, sigma)
    return _norm_cdf(d2)


def black76_price(F: float, K: float, T: float, r: float, sigma: float, option_type: OptionType) -> float:
    """
    Black-76 theoretical price of a European option on a future.

    Args:
        F: Futures price of the underlying (NOT spot).
        K: Strike price.
        T: Time to expiry, in YEARS (e.g. 7 days = 7/365).
        r: Risk-free rate, as a decimal (e.g. 0.065 for 6.5%).
        sigma: Volatility, as a decimal (e.g. 0.20 for 20%).
        option_type: 'CE' (call) or 'PE' (put).
    """
    d1, d2 = _d1_d2(F, K, T, sigma)
    discount = math.exp(-r * T)
    if option_type == "CE":
        return discount * (F * _norm_cdf(d1) - K * _norm_cdf(d2))
    return discount * (K * _norm_cdf(-d2) - F * _norm_cdf(-d1))


# Intrinsic value at T=0 (or when T is too small to solve numerically) —
# used as the floor a solved IV's implied price must clear, and as the
# degenerate-case return value.
def _intrinsic(F: float, K: float, option_type: OptionType) -> float:
    return max(F - K, 0.0) if option_type == "CE" else max(K - F, 0.0)


_IV_MIN, _IV_MAX = 0.001, 5.0  # 0.1% to 500% — wide enough for any real NSE contract, including near-expiry blowouts.
_IV_NEWTON_MAX_ITER = 50
_IV_NEWTON_TOL = 1e-6
_IV_BISECT_MAX_ITER = 100


def implied_volatility(
    F: float, K: float, T: float, r: float, market_price: float, option_type: OptionType,
) -> Optional[float]:
    """
    Solve for the volatility that makes black76_price(...) match a real
    traded market_price — Newton-Raphson first (fast, uses vega as the
    derivative), falling back to bisection (slower but always converges
    for a well-posed problem) if Newton diverges or oscillates.

    Returns None (not an exception) if no solution exists — e.g.
    market_price is below intrinsic value (a bad/stale print) or T <= 0
    (already expired) — these are normal data-quality outcomes for real
    market data, not programming errors.
    """
    if T <= 0 or F <= 0 or K <= 0:
        return None
    intrinsic = _intrinsic(F, K, option_type)
    if market_price < intrinsic - 1e-6:
        return None  # Below intrinsic — arbitrage-violating print, can't solve.
    if market_price <= 0:
        return None

    # Newton-Raphson, seeded at a typical NSE index/stock IV.
    sigma = 0.30
    for _ in range(_IV_NEWTON_MAX_ITER):
        try:
            price = black76_price(F, K, T, r, sigma, option_type)
            v = vega(F, K, T, r, sigma)
        except ValueError:
            break
        diff = price - market_price
        if abs(diff) < _IV_NEWTON_TOL:
            return sigma
        if v < 1e-8:
            break  # Vega too flat — Newton step would be unstable, fall through to bisection.
        sigma -= diff / v
        if sigma <= 0 or sigma > _IV_MAX:
            break  # Stepped out of bounds — abandon Newton, fall through to bisection.

    # Bisection fallback — monotonic in sigma, so this always converges
    # for a market_price that's actually reachable within [_IV_MIN, _IV_MAX].
    lo, hi = _IV_MIN, _IV_MAX
    price_lo = black76_price(F, K, T, r, lo, option_type)
    price_hi = black76_price(F, K, T, r, hi, option_type)
    if not (price_lo <= market_price <= price_hi):
        return None  # market_price unreachable in the sane IV range — bad print.
    for _ in range(_IV_BISECT_MAX_ITER):
        mid = (lo + hi) / 2.0
        price_mid = black76_price(F, K, T, r, mid, option_type)
        if abs(price_mid - market_price) < _IV_NEWTON_TOL:
            return mid
        if price_mid < market_price:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def vega(F: float, K: float, T: float, r: float, sigma: float) -> float:
    """Same formula for calls and puts. Per 1.00 (100%) change in sigma — divide by 100 for 'per 1% IV point'."""
    d1, _ = _d1_d2(F, K, T, sigma)
    return math.exp(-r * T) * F * _norm_pdf(d1) * math.sqrt(T)


class Greeks(TypedDict):
    iv: float
    delta: float
    gamma: float
    theta: float  # Per calendar day (already divided by 365), not per year.
    vega: float   # Per 1 percentage-point change in IV (already divided by 100).
    rho: float    # Per 1 percentage-point change in rate (already divided by 100).


def greeks(F: float, K: float, T: float, r: float, sigma: float, option_type: OptionType) -> Greeks:
    """
    All 5 standard Greeks from the Black-76 closed-form derivatives.

    Delta is quoted directly against the futures price (no forward-price
    adjustment needed — this is one of the simplifications Black-76 gives
    over Black-Scholes). Gamma and Vega are identical for calls and puts;
    Delta, Theta, and Rho differ by side.
    """
    d1, d2 = _d1_d2(F, K, T, sigma)
    discount = math.exp(-r * T)
    sqrt_t = math.sqrt(T)
    price = black76_price(F, K, T, r, sigma, option_type)

    # d1/d2 have no r-dependence in Black-76 (cost-of-carry b=0 removes r
    # from the drift term entirely) — the ONLY place r enters the price is
    # the e^{-rT} discount factor, so rho collapses to the simple
    # closed form below (verified against Haug's generalized
    # Black-Scholes-Merton formulas with b=0), not the more complex
    # Black-Scholes rho.
    rho = -T * price / 100.0

    if option_type == "CE":
        delta = discount * _norm_cdf(d1)
        theta_annual = (
            -F * discount * _norm_pdf(d1) * sigma / (2 * sqrt_t)
            + r * discount * F * _norm_cdf(d1)
            - r * discount * K * _norm_cdf(d2)
        )
    else:
        delta = -discount * _norm_cdf(-d1)
        theta_annual = (
            -F * discount * _norm_pdf(d1) * sigma / (2 * sqrt_t)
            - r * discount * F * _norm_cdf(-d1)
            + r * discount * K * _norm_cdf(-d2)
        )

    gamma_val = discount * _norm_pdf(d1) / (F * sigma * sqrt_t)
    vega_val = vega(F, K, T, r, sigma) / 100.0

    return {
        "iv": round(sigma * 100.0, 4),
        "delta": round(delta, 6),
        "gamma": round(gamma_val, 8),
        "theta": round(theta_annual / 365.0, 6),
        "vega": round(vega_val, 6),
        "rho": round(rho, 6),
    }


def compute_greeks_from_market_price(
    F: float, K: float, T: float, r: float, market_price: float, option_type: OptionType,
) -> Optional[Greeks]:
    """
    The function most callers want: solve IV against a real traded price,
    then return all 5 Greeks computed from that solved IV. Returns None
    if IV can't be solved (see implied_volatility()) — callers must treat
    that as "no Greeks available for this leg right now", not an error.
    """
    sigma = implied_volatility(F, K, T, r, market_price, option_type)
    if sigma is None:
        return None
    return greeks(F, K, T, r, sigma, option_type)
