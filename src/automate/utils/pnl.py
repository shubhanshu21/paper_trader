"""
utils/pnl.py — Shared gross/net P&L + charges calculator for a short strangle
position (two SELL legs opened together, closed together).

Single source of truth for the "gross P&L minus real Indian F&O transaction
costs" formula, used by every live/paper P&L surface (open-position MTM, the
websocket feed, closed positions, the dashboard, the leaderboard) so they
can't independently drift the way they did before (each one re-derived its
own gross-only formula).
"""
from automate.utils.costs import calculate_options_transaction_cost_breakdown, sum_breakdowns


def compute_strangle_pnl(
    call_entry: float, put_entry: float, call_exit: float, put_exit: float, quantity: int,
    rates: dict = None,
) -> dict:
    """
    gross/net P&L (₹) and the combined charges breakdown across all 4 legs
    (call+put entry SELL, call+put exit BUY) of a short strangle.

    `call_exit`/`put_exit` may be either an actual closing fill price (closed
    position) or the current LTP (mark-to-market on an open position) — the
    math is identical either way.

    `rates`: this account's charge-rate overrides — see
    utils/wallet.py's get_charge_rates(). Defaults to utils/costs.py's
    DEFAULT_RATES when not passed.
    """
    gross_pnl = (call_entry - call_exit + put_entry - put_exit) * quantity

    call_entry_c = calculate_options_transaction_cost_breakdown(call_entry, quantity, "SELL", rates)
    put_entry_c = calculate_options_transaction_cost_breakdown(put_entry, quantity, "SELL", rates)
    call_exit_c = calculate_options_transaction_cost_breakdown(call_exit, quantity, "BUY", rates)
    put_exit_c = calculate_options_transaction_cost_breakdown(put_exit, quantity, "BUY", rates)
    charges = sum_breakdowns(call_entry_c, put_entry_c, call_exit_c, put_exit_c)

    return {
        "gross_pnl": round(gross_pnl, 2),
        "net_pnl": round(gross_pnl - charges["total"], 2),
        "charges": charges,
    }


def entry_charges_only(call_entry: float, put_entry: float, quantity: int, rates: dict = None) -> dict:
    """Charges already incurred on an open position (entry SELL legs only, no exit yet)."""
    call_entry_c = calculate_options_transaction_cost_breakdown(call_entry, quantity, "SELL", rates)
    put_entry_c = calculate_options_transaction_cost_breakdown(put_entry, quantity, "SELL", rates)
    return sum_breakdowns(call_entry_c, put_entry_c)


def compute_basket_pnl(legs: list, rates: dict = None) -> dict:
    """
    Generalization of compute_strangle_pnl() for a Custom Strategy Builder
    basket of N arbitrary BUY/SELL legs (not just a fixed 2-leg short
    strangle) — see db.models.CustomStrategyPosition, which stores exactly
    this shape (one row per leg, entry_price/exit_price + transaction_type).

    Each item in `legs` needs: entry_price, exit_price (or current LTP for
    mark-to-market), quantity, transaction_type ('BUY'|'SELL').
    """
    gross_pnl = 0.0
    charge_breakdowns = []
    for leg in legs:
        entry = float(leg["entry_price"])
        exit_ = float(leg["exit_price"])
        qty = leg["quantity"]
        sign = 1 if leg["transaction_type"] == "SELL" else -1  # SELL profits when price falls
        gross_pnl += (entry - exit_) * qty * sign

        exit_transaction_type = "BUY" if leg["transaction_type"] == "SELL" else "SELL"
        charge_breakdowns.append(calculate_options_transaction_cost_breakdown(entry, qty, leg["transaction_type"], rates))
        charge_breakdowns.append(calculate_options_transaction_cost_breakdown(exit_, qty, exit_transaction_type, rates))

    charges = sum_breakdowns(*charge_breakdowns) if charge_breakdowns else {"total": 0.0}
    return {
        "gross_pnl": round(gross_pnl, 2),
        "net_pnl": round(gross_pnl - charges["total"], 2),
        "charges": charges,
    }
