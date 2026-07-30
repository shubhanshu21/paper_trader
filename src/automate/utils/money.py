"""
utils/money.py — Indian Rupee formatting (lakh/crore comma grouping, e.g. ₹13,42,792.51).
"""


def format_inr(x: float, *, signed: bool = True) -> str:
    """Format a number as Indian Rupees using Indian digit grouping (lakhs/crores)."""
    sign = "-" if x < 0 else ("+" if signed else "")
    whole, _, dec = f"{abs(x):.2f}".partition(".")

    if len(whole) <= 3:
        grouped = whole
    else:
        last3, rest = whole[-3:], whole[:-3]
        parts = []
        while len(rest) > 2:
            parts.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            parts.insert(0, rest)
        grouped = ",".join(parts) + "," + last3

    return f"{sign}₹{grouped}.{dec}"
