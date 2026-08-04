"""
utils/option_utils.py — Pure mathematical helpers for options strategy logic.

Contains no broker-specific code so these functions are independently testable.
"""

import logging
from datetime import date, datetime, timedelta

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Strike rounding
# ---------------------------------------------------------------------------

def round_to_nearest_strike(price: float, step: float) -> float:
    """
    Snap a floating-point price to the nearest valid NSE options strike.

    NSE stock options have strikes at fixed intervals (e.g., every ₹20 for
    RELIANCE, every ₹50 for TCS — though some lower-priced stocks have a
    FRACTIONAL interval, e.g. ₹2.5). This rounds to the closest multiple
    of `step`.

    Returns a plain int when the result is a whole number (the common
    case), or a float when `step` itself is fractional — wrapping in
    int() unconditionally would silently truncate a real listed strike
    like 152.5 down to 152, which isn't even a valid strike.

    Args:
        price: The raw calculated price (e.g., spot × 1.10).
        step:  The strike interval for the underlying (e.g., 20, 50, 2.5).

    Returns:
        The nearest valid strike.

    Examples:
        >>> round_to_nearest_strike(1540.3, 20)
        1540
        >>> round_to_nearest_strike(1549.9, 20)
        1560
        >>> round_to_nearest_strike(151.3, 2.5)
        152.5
    """
    if step <= 0:
        raise ValueError(f"Strike step must be positive, got {step}.")
    rounded = round(step * round(price / step), 4)
    if rounded == int(rounded):
        rounded = int(rounded)
    log.debug("Rounded price %.2f → strike %s (step=%s)", price, rounded, step)
    return rounded


# ---------------------------------------------------------------------------
# Expiry selection
# ---------------------------------------------------------------------------

def find_nearest_monthly_expiry(
    expiries: list[str], fmt: str = "%Y-%m-%d", reference_date: date | None = None,
) -> str | None:
    """
    Given a list of expiry date strings, return the nearest one that is
    on or after `reference_date` (i.e., the current or next monthly expiry).

    The Upstox option chain API returns expiry dates as ISO strings like
    '2025-07-31'. This function finds the soonest one at/after the
    reference date.

    Args:
        expiries:       List of expiry date strings from the option chain.
        fmt:            strptime format string matching the date strings.
        reference_date: The date to measure "future" from. None (default)
            = real wall-clock today — correct for live trading. Backtests
            simulating a past date MUST pass the simulated date here, or
            every historical expiry will be (wrongly) treated as already
            lapsed relative to whenever the backtest happens to actually run.

    Returns:
        The soonest future expiry string, or None if no future expiry found.
    """
    today = reference_date if reference_date is not None else date.today()
    future_expiries = []

    for exp_str in expiries:
        try:
            exp_date = datetime.strptime(exp_str, fmt).date()
            if exp_date >= today:
                future_expiries.append((exp_date, exp_str))
        except ValueError:
            log.warning("Skipping unparseable expiry string: '%s'", exp_str)

    if not future_expiries:
        log.error("No future expiries found in list: %s", expiries)
        return None

    # Sort ascending and return the nearest
    future_expiries.sort(key=lambda x: x[0])
    nearest = future_expiries[0][1]
    log.info("Selected nearest expiry: %s (from %d options)", nearest, len(future_expiries))
    return nearest


def find_nearest_expiry_by_type(
    expiries: list[str], mode: str, fmt: str = "%Y-%m-%d", reference_date: date | None = None,
) -> str | None:
    """
    Like find_nearest_monthly_expiry(), but expiry-type aware — needed
    once an underlying (NIFTY, BANKNIFTY, ...) has BOTH weekly and monthly
    contracts listed simultaneously, which find_nearest_monthly_expiry()
    doesn't distinguish (it just returns whichever is soonest, which is
    usually a weekly one).

    "Monthly" is standard NSE convention: the LAST expiry listed within a
    given calendar month for that underlying (no separate flag exists —
    every expiry list already contains this, it just needs grouping).
    Everything else is "weekly". A symbol with no weekly contracts (most
    stocks) has every expiry double as its own month's monthly expiry, so
    mode="WEEKLY" and mode="MONTHLY" naturally converge to the same
    answer there — no special-casing needed for stocks vs indices.

    Args:
        mode: "WEEKLY" (soonest expiry of any kind) or "MONTHLY" (soonest
            expiry that is the LAST one in its calendar month).

    Returns:
        The selected expiry string, or None if none qualify.
    """
    today = reference_date if reference_date is not None else date.today()
    parsed: list[tuple[date, str]] = []
    for exp_str in expiries:
        try:
            parsed.append((datetime.strptime(exp_str, fmt).date(), exp_str))
        except ValueError:
            log.warning("Skipping unparseable expiry string: '%s'", exp_str)
    if not parsed:
        return None

    if mode == "WEEKLY":
        future = sorted(d for d, _ in parsed if d >= today)
        if not future:
            return None
        return future[0].isoformat()

    # MONTHLY — group ALL expiries (not just future ones) by (year, month)
    # so the last-in-month determination isn't skewed by already-lapsed
    # dates being excluded first.
    by_month: dict[tuple[int, int], date] = {}
    for d, _ in parsed:
        key = (d.year, d.month)
        if key not in by_month or d > by_month[key]:
            by_month[key] = d
    monthly_dates = sorted(d for d in by_month.values() if d >= today)
    if not monthly_dates:
        return None
    return monthly_dates[0].isoformat()


# ---------------------------------------------------------------------------
# Option chain parser
# ---------------------------------------------------------------------------

def find_instrument_token(
    chain_data: list,
    target_strike: float,
    option_type: str,
) -> str | None:
    """
    Search an Upstox option chain response for the instrument token that
    matches a specific strike price and option type (CE or PE).

    The Upstox `get_put_call_option_chain` API returns a list of
    `PutCallOptionChainData` objects with `.call_options` and `.put_options`
    attributes. Each leg has an `.option_greeks` dict and a `.market_data`
    object containing the `instrument_token`.

    Args:
        chain_data:    List of PutCallOptionChainData objects from the API.
        target_strike: The strike price we're searching for — usually a
            whole number, but can be fractional for lower-priced stocks
            (e.g. 152.5).
        option_type:   'CE' for Call or 'PE' for Put.

    Returns:
        The instrument token string (e.g., 'NSE_FO|12345'), or None if
        no match is found.
    """
    option_type = option_type.upper()
    if option_type not in ("CE", "PE"):
        raise ValueError(f"option_type must be 'CE' or 'PE', got '{option_type}'.")

    available_legs = []
    for item in chain_data:
        try:
            strike = getattr(item, "strike_price", None)
            if strike is None:
                continue

            # round() not int(): a bare int() truncation would silently
            # turn a real fractional strike (e.g. 152.5, common for
            # lower-priced stocks) into 152 — not even a valid strike —
            # and it would then never exact-match target_strike.
            parsed_strike = round(float(strike), 2)
            leg = None
            if option_type == "CE" and item.call_options:
                leg = item.call_options
            elif option_type == "PE" and item.put_options:
                leg = item.put_options
            else:
                continue

            token = getattr(leg, "instrument_key", None)
            if token:
                available_legs.append((parsed_strike, token))

        except AttributeError as exc:
            log.debug("Skipping malformed chain entry: %s", exc)
            continue

    if not available_legs:
        log.error("No valid options found in the option chain for type %s.", option_type)
        return None

    # Sort available legs by strike price
    available_legs.sort(key=lambda x: x[0])

    # 1. Look for the exact strike match
    for strike, token in available_legs:
        if strike == round(float(target_strike), 2):
            log.info("Found exact instrument token for %s %s: %s", target_strike, option_type, token)
            return token

    # 2. Fallback: Find the closest strike in the chain
    closest_strike, closest_token = min(available_legs, key=lambda x: abs(x[0] - target_strike))
    diff = abs(closest_strike - target_strike)

    # Allow fallback if the difference is within 2.5% of the target strike
    if diff <= (target_strike * 0.025):
        log.warning(
            "Exact strike %s %s not found in chain. Falling back to nearest available strike: %s %s (diff: %s)",
            target_strike, option_type, closest_strike, option_type, diff
        )
        return closest_token

    log.error(
        "No instrument token found for strike %s %s in the option chain. "
        "Nearest was %s %s (diff %s exceeds threshold). Available strikes: %s",
        target_strike, option_type, closest_strike, option_type, diff,
        [x[0] for x in available_legs]
    )
    return None


def find_leg_iv(chain_data: list, target_strike: float, option_type: str) -> float | None:
    """
    Exact-strike-only lookup of Upstox's OWN implied volatility for one
    option leg, straight from the option chain response's `.option_greeks.iv`
    field (an `AnalyticsData` object — same one their `.delta`/`.gamma`/
    `.theta`/`.vega` also come from) — the broker's real computed IV,
    not a value solved from a single LTP tick.

    Deliberately exact-match only (no nearby-strike fallback, unlike
    find_instrument_token()) — an IV borrowed from a different strike due
    to a chain gap would be actively misleading for probability math,
    whereas a missing token can at least be reported as an error.
    Returns a decimal (e.g. 0.22 for 22%), or None if not found/not
    populated (some illiquid contracts have a real LTP but no IV yet).
    """
    option_type = option_type.upper()
    target = round(float(target_strike), 2)
    for item in chain_data:
        strike = getattr(item, "strike_price", None)
        if strike is None or round(float(strike), 2) != target:
            continue
        leg = item.call_options if option_type == "CE" else item.put_options
        greeks = getattr(leg, "option_greeks", None) if leg else None
        iv = getattr(greeks, "iv", None) if greeks else None
        if iv is None:
            return None
        return float(iv) / 100.0
    return None


# ---------------------------------------------------------------------------
# Band calculator
# ---------------------------------------------------------------------------

def calculate_strangle_strikes(
    spot_price: float,
    band_pct: float,
    strike_step: float,
):
    """
    Calculate the upper (CE) and lower (PE) strikes for a short strangle.

    The strategy sells a Call `band_pct`% above spot and a Put `band_pct`%
    below spot, rounded to the nearest valid strike interval.

    Args:
        spot_price:  Current LTP of the underlying stock.
        band_pct:    Percentage band (e.g., 0.10 for 10%).
        strike_step: Strike interval (e.g., 20, 50, 2.5).

    Returns:
        Tuple of (call_strike, put_strike) — int for a whole-number
        result (the common case), float if strike_step is fractional.
    """
    call_raw = spot_price * (1 + band_pct)
    put_raw = spot_price * (1 - band_pct)

    call_strike = round_to_nearest_strike(call_raw, strike_step)
    put_strike = round_to_nearest_strike(put_raw, strike_step)

    log.info(
        "Spot: %.2f | Band: %.0f%% | CE strike: %s (raw: %.2f) | PE strike: %s (raw: %.2f)",
        spot_price, band_pct * 100, call_strike, call_raw, put_strike, put_raw,
    )
    return call_strike, put_strike


# ---------------------------------------------------------------------------
# Stop-loss / take-profit trigger check (short strangle)
# ---------------------------------------------------------------------------

def strangle_pnl_pct(
    call_entry_price: float, put_entry_price: float,
    call_current_price: float, put_current_price: float,
) -> float:
    """
    Current P&L on a short strangle, as a percentage OF THE PREMIUM
    COLLECTED at entry — the standard convention for option-selling exit
    rules (e.g. "take profit at +60%" = close once 60% of the max possible
    profit is captured; "stop-loss at -200%" = close once the loss reaches
    2x the premium collected).

    Positive = profit (both legs have gotten cheaper to buy back).
    Negative = loss (legs have gotten more expensive).

    Args:
        call_entry_price, put_entry_price: Prices the legs were SOLD at.
        call_current_price, put_current_price: Prices to BUY them back now.

    Returns:
        P&L as a percentage of premium collected, e.g. 45.0 = +45%,
        -180.0 = -180% (lost 1.8x the premium). Returns 0.0 if no premium
        was collected (degenerate/invalid entry — avoids a ZeroDivisionError).
    """
    premium_collected = call_entry_price + put_entry_price
    if premium_collected <= 0:
        return 0.0
    cost_to_close = call_current_price + put_current_price
    return (premium_collected - cost_to_close) / premium_collected * 100.0


def check_exit_trigger(
    pnl_pct: float,
    take_profit_pct: float | None,
    stop_loss_pct: float | None,
) -> str | None:
    """
    Decide whether a position should be closed early, given its current
    P&L (as % of premium collected — see strangle_pnl_pct()) and the
    configured thresholds. Either threshold may be None (disabled).

    Args:
        pnl_pct:          Current P&L %, from strangle_pnl_pct().
        take_profit_pct:  Exit once pnl_pct >= this (e.g. 60.0 = +60%). None disables.
        stop_loss_pct:    Exit once pnl_pct <= -this (e.g. 200.0 = -200%). Pass a
                           POSITIVE number for the magnitude of the allowed loss.
                           None disables.

    Returns:
        "TAKE_PROFIT", "STOP_LOSS", or None (no trigger).
    """
    if take_profit_pct is not None and pnl_pct >= take_profit_pct:
        return "TAKE_PROFIT"
    if stop_loss_pct is not None and pnl_pct <= -abs(stop_loss_pct):
        return "STOP_LOSS"
    return None


# ---------------------------------------------------------------------------
# Multi-leg payoff / breakeven (spot-based stop-loss for N-leg structures)
# ---------------------------------------------------------------------------

def strategy_payoff_at_expiry(spot: float, legs: list[dict]) -> float:
    """
    Total INTRINSIC-value P&L of an N-leg option position if the
    underlying settled at `spot` at expiry — the standard basis for a
    "breakeven" (ignores remaining time value, which is exactly what
    "breakeven" conventionally means for an options structure).

    Each leg dict needs: strike, option_type ('CE'/'PE'),
    transaction_type ('BUY'/'SELL'), entry_price, quantity. EQUITY legs
    (no strike) are skipped — they have no expiry-payoff kink and aren't
    part of what "breakeven" means for the options structure wrapped
    around them.
    """
    total = 0.0
    for leg in legs:
        strike = leg.get("strike")
        option_type = leg.get("option_type")
        if strike is None or option_type is None:
            continue
        intrinsic = max(spot - strike, 0.0) if option_type == "CE" else max(strike - spot, 0.0)
        entry_price = float(leg["entry_price"])
        quantity = leg["quantity"]
        pnl_per_unit = (intrinsic - entry_price) if leg["transaction_type"] == "BUY" else (entry_price - intrinsic)
        total += pnl_per_unit * quantity
    return total


def find_breakevens(legs: list[dict]) -> list[float]:
    """
    Every spot price at which `strategy_payoff_at_expiry` crosses zero,
    ascending — a multi-leg, mixed-quantity structure (e.g. a straddle +
    a bigger strangle + even-bigger deep-OTM wings) can have more than
    the textbook two, so this returns however many actually exist rather
    than assuming a fixed shape.

    Exact, not a coarse numeric scan: the payoff is piecewise LINEAR in
    spot with kinks only at each leg's own strike (an option's intrinsic
    value is linear on each side of its strike), so evaluating only at
    the strikes (plus far-below/far-above bounds) and interpolating
    between consecutive points is exact — no grid-resolution tradeoff.

    Returns [] if there are no OPTION legs (nothing to find a breakeven
    of).
    """
    strikes = sorted({leg["strike"] for leg in legs if leg.get("strike") is not None and leg.get("option_type") is not None})
    if not strikes:
        return []

    # Bounds far enough past the outermost strikes that intrinsic value
    # there is a straight line with no further kinks in between.
    span = max(strikes[-1] - strikes[0], strikes[0] * 0.5, 1000.0)
    points = [strikes[0] - span] + strikes + [strikes[-1] + span]

    breakevens: list[float] = []
    prev_x, prev_y = points[0], strategy_payoff_at_expiry(points[0], legs)
    for x in points[1:]:
        y = strategy_payoff_at_expiry(x, legs)
        if prev_y == 0.0:
            breakevens.append(prev_x)
        elif (prev_y < 0) != (y < 0):
            breakevens.append(prev_x + (0.0 - prev_y) * (x - prev_x) / (y - prev_y))
        prev_x, prev_y = x, y
    if prev_y == 0.0:
        breakevens.append(prev_x)
    return breakevens


def is_within_pre_expiry_buffer(today: date, expiry: date, exit_days_before_expiry: int) -> bool:
    """
    True once `today` has reached the position's own pre-expiry exit
    buffer — i.e. it's time to force-close regardless of SL/TP state, per
    run_position_monitor.py's expiry safety net. Deliberately never waits
    until expiry day itself: `exit_days_before_expiry` days earlier is the
    real deadline (see config.py's TenPercentOTMStrangleConfig.
    EXIT_DAYS_BEFORE_EXPIRY for why — expiry-day gamma risk and, for stock
    options, compulsory physical settlement).

    Plain calendar-day arithmetic, same as the rest of this module's
    expiry handling — the actual close still only ever happens on a real
    trading day, since callers only run this check on days the market's
    open.
    """
    exit_by = expiry - timedelta(days=exit_days_before_expiry)
    return today >= exit_by
