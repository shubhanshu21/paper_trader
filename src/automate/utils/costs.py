"""
utils/costs.py — Realistic Transaction Cost Calculator

Calculates Indian market taxes, brokerage, and exchange fees for F&O Options.
Taxes apply to the premium turnover (Price × Quantity).
"""

def calculate_options_transaction_cost_breakdown(
    price: float,
    quantity: int,
    transaction_type: str = "SELL",
    brokerage_per_order: float = 20.0,
) -> dict:
    """
    Calculate the itemised transaction costs for an options trade (NSE).

    Args:
        price: The execution premium of the option.
        quantity: The total number of units (lot_size * lots).
        transaction_type: "BUY" or "SELL". STT only applies to SELL for options.
        brokerage_per_order: Flat brokerage fee per executed order (e.g., ₹20 for Upstox).

    Returns:
        {"brokerage", "exchange_charges", "gst", "stt", "sebi_charges",
         "stamp_duty", "total"} in ₹, each rounded to paise.
    """
    turnover = price * quantity

    # 1. Brokerage (Flat fee per order)
    brokerage = brokerage_per_order

    # 2. Exchange Transaction Charges (NSE Options is approx 0.03503% on premium)
    exchange_charges = turnover * 0.0003503

    # 3. GST (18% on Brokerage + Exchange Transaction Charges)
    gst = (brokerage + exchange_charges) * 0.18

    # 4. STT (Securities Transaction Tax)
    # 0.15% on the SELL side premium, effective 1 Apr 2026 (NSE Circular
    # Ref. 02/2026), hiked from the prior 0.10% rate. STT on options is
    # charged to the seller only (buy-to-close/unwind trades are not
    # taxed) unless the option is exercised, which this codebase's
    # pre-expiry auto-unwind path never triggers.
    stt = 0.0
    if transaction_type.upper() == "SELL":
        stt = turnover * 0.0015

    # 5. SEBI Charges (₹10 per crore -> 0.0001%)
    sebi_charges = turnover * 0.000001

    # 6. Stamp Duty (0.003% on BUY side premium)
    stamp_duty = 0.0
    if transaction_type.upper() == "BUY":
        stamp_duty = turnover * 0.00003

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
    brokerage_per_order: float = 20.0,
) -> float:
    """Total-only convenience wrapper around calculate_options_transaction_cost_breakdown()."""
    return calculate_options_transaction_cost_breakdown(
        price, quantity, transaction_type, brokerage_per_order
    )["total"]


_BREAKDOWN_KEYS = ("brokerage", "exchange_charges", "gst", "stt", "sebi_charges", "stamp_duty", "total")


def sum_breakdowns(*breakdowns: dict) -> dict:
    """Component-wise sum of any number of cost breakdowns (e.g. all 4 legs of a strangle)."""
    return {k: round(sum(b[k] for b in breakdowns), 2) for k in _BREAKDOWN_KEYS}
