"""
utils/costs.py — Realistic Transaction Cost Calculator

Calculates Indian market taxes, brokerage, and exchange fees for F&O Options.
Taxes apply to the premium turnover (Price × Quantity).

Rates default to the current NSE/SEBI/govt schedule (see DEFAULT_RATES) but
are overridable per call via the `rates` dict — see utils/wallet.py's
get_charge_rates()/set_charge_rates(), which persist a per-user override in
WalletSettings so these can be corrected from the Profile page whenever the
exchange/government revises them, without a code deploy.
"""

# Current NSE/SEBI/govt schedule as of the STT hike effective 1 Apr 2026
# (NSE Circular Ref. 02/2026) and the transaction-charge circular effective
# 1 Mar 2026 (net rate unchanged — that circular only reallocated the IPFT
# contribution, per NSE's own "no change in overall outflow" wording).
DEFAULT_RATES = {
    "brokerage_per_order": 20.0,     # ₹ flat per executed order (e.g. Upstox)
    "exchange_charge_pct": 0.0003503,  # NSE options transaction charge, on premium turnover
    "gst_pct": 0.18,                 # on (brokerage + exchange charges + SEBI charges)
    "stt_pct": 0.0015,               # options STT, SELL side only, on premium turnover
    "sebi_charge_pct": 0.000001,     # ₹10 / crore
    "stamp_duty_pct": 0.00003,       # stamp duty, BUY side only, on premium turnover
}


def calculate_options_transaction_cost_breakdown(
    price: float,
    quantity: int,
    transaction_type: str = "SELL",
    rates: dict | None = None,
) -> dict:
    """
    Calculate the itemised transaction costs for an options trade (NSE).

    Args:
        price: The execution premium of the option.
        quantity: The total number of units (lot_size * lots).
        transaction_type: "BUY" or "SELL". STT only applies to SELL for options.
        rates: Optional overrides for any of DEFAULT_RATES's keys (e.g. a
            user's own rates from get_charge_rates()) — missing keys fall
            back to DEFAULT_RATES.

    Returns:
        {"brokerage", "exchange_charges", "gst", "stt", "sebi_charges",
         "stamp_duty", "total"} in ₹, each rounded to paise.
    """
    r = {**DEFAULT_RATES, **(rates or {})}
    turnover = price * quantity

    # 1. Brokerage (Flat fee per order)
    brokerage = r["brokerage_per_order"]

    # 2. Exchange Transaction Charges (on premium)
    exchange_charges = turnover * r["exchange_charge_pct"]

    # 3. SEBI Charges
    sebi_charges = turnover * r["sebi_charge_pct"]

    # 4. GST (on Brokerage + Exchange Transaction Charges + SEBI charges —
    # STT and stamp duty are taxes, not a taxable service fee, so they're
    # excluded from this base.)
    gst = (brokerage + exchange_charges + sebi_charges) * r["gst_pct"]

    # 5. STT (Securities Transaction Tax)
    # Charged to the seller only (buy-to-close/unwind trades are not taxed)
    # unless the option is exercised, which this codebase's pre-expiry
    # auto-unwind path never triggers.
    stt = 0.0
    if transaction_type.upper() == "SELL":
        stt = turnover * r["stt_pct"]

    # 6. Stamp Duty (BUY side only)
    stamp_duty = 0.0
    if transaction_type.upper() == "BUY":
        stamp_duty = turnover * r["stamp_duty_pct"]

    total = brokerage + exchange_charges + gst + stt + sebi_charges + stamp_duty
    return {
        "brokerage": round(brokerage, 2),
        "exchange_charges": round(exchange_charges, 2),
        "gst": round(gst, 2),
        "stt": round(stt, 2),
        "sebi_charges": round(sebi_charges, 2),
        "stamp_duty": round(stamp_duty, 2),
        "total": round(total, 2),
    }


def calculate_options_transaction_cost(
    price: float,
    quantity: int,
    transaction_type: str = "SELL",
    rates: dict | None = None,
) -> float:
    """Total-only convenience wrapper around calculate_options_transaction_cost_breakdown()."""
    return calculate_options_transaction_cost_breakdown(
        price, quantity, transaction_type, rates
    )["total"]


def calculate_equity_transaction_cost_breakdown(
    price: float,
    quantity: int,
    transaction_type: str = "SELL",
    rates: dict | None = None,
) -> dict:
    """
    Itemised transaction costs for an EQUITY DELIVERY trade (NSE cash
    market) — same shape/keys AND same rate SOURCE as
    calculate_options_transaction_cost_breakdown() (DEFAULT_RATES, with
    whatever per-user overrides utils/wallet.py's get_charge_rates()
    layers on — the same values the Profile page's "Transaction Cost
    Rates" form already edits, see WalletSettings). Deliberately NOT a
    second hardcoded rate table: this app only has ONE user-configurable
    rate slot per component today, so a strategy that mixes EQUITY and
    OPTION legs sees ONE consistent set of numbers, and a rate edited on
    the Profile page is reflected everywhere costs are computed — not
    just for options.

    The one REAL behavioral difference from options (not just a rate
    value) is STT: options STT is sell-side only, equity delivery STT
    applies to BOTH sides — that's a tax-rule difference, not something a
    rate override could express, so it's hardcoded here as logic.

    Args:
        price: The execution price per share.
        quantity: Number of shares.
        transaction_type: "BUY" or "SELL" — determines which side stamp
            duty applies to (buy-only, same rule as options); STT,
            unlike options, applies regardless of side.
        rates: Optional overrides for any of DEFAULT_RATES's keys (e.g. a
            user's own rates from get_charge_rates()) — missing keys fall
            back to DEFAULT_RATES, identical to the options function.

    Returns:
        {"brokerage", "exchange_charges", "gst", "stt", "sebi_charges",
         "stamp_duty", "total"} in ₹, each rounded to paise.
    """
    r = {**DEFAULT_RATES, **(rates or {})}
    turnover = price * quantity

    brokerage = r["brokerage_per_order"]
    exchange_charges = turnover * r["exchange_charge_pct"]
    sebi_charges = turnover * r["sebi_charge_pct"]
    gst = (brokerage + exchange_charges + sebi_charges) * r["gst_pct"]
    stt = turnover * r["stt_pct"]  # both sides, unlike options
    stamp_duty = turnover * r["stamp_duty_pct"] if transaction_type.upper() == "BUY" else 0.0

    total = brokerage + exchange_charges + gst + stt + sebi_charges + stamp_duty
    return {
        "brokerage": round(brokerage, 2),
        "exchange_charges": round(exchange_charges, 2),
        "gst": round(gst, 2),
        "stt": round(stt, 2),
        "sebi_charges": round(sebi_charges, 2),
        "stamp_duty": round(stamp_duty, 2),
        "total": round(total, 2),
    }


def calculate_leg_transaction_cost_breakdown(
    instrument_type: str,
    price: float,
    quantity: int,
    transaction_type: str = "SELL",
    rates: dict | None = None,
) -> dict:
    """
    Dispatches to calculate_equity_transaction_cost_breakdown() for an
    EQUITY leg, calculate_options_transaction_cost_breakdown() for
    anything else (OPTION, or an unset/legacy row — matches this
    codebase's existing "instrument_type or OPTION" default convention,
    see rule_schema.py). The one function every CustomStrategyPosition
    cost call site should use instead of hardcoding the options one —
    that hardcoding is exactly what silently mispriced every EQUITY leg's
    real P&L (wrong-side STT) before this function existed.
    """
    if instrument_type == "EQUITY":
        return calculate_equity_transaction_cost_breakdown(price, quantity, transaction_type, rates)
    return calculate_options_transaction_cost_breakdown(price, quantity, transaction_type, rates)


_BREAKDOWN_KEYS = ("brokerage", "exchange_charges", "gst", "stt", "sebi_charges", "stamp_duty", "total")


def sum_breakdowns(*breakdowns: dict) -> dict:
    """Component-wise sum of any number of cost breakdowns (e.g. all 4 legs of a strangle)."""
    return {k: round(sum(b[k] for b in breakdowns), 2) for k in _BREAKDOWN_KEYS}
