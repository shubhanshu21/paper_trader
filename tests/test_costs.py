"""
tests/test_costs.py — Transaction cost model tests for utils/costs.py.
Pure math, no network/credentials needed. Locks in the current verified
rates (brokerage ₹20 flat, exchange 0.03503%, GST 18%, STT 0.15% sell-side,
SEBI 0.0001%, stamp duty 0.003% buy-side) so a future edit can't silently
drift from what was verified against real sources.
"""
from automate.utils.costs import calculate_options_transaction_cost


def test_sell_side_includes_stt_not_stamp_duty():
    cost = calculate_options_transaction_cost(price=100.0, quantity=500, transaction_type="SELL")
    turnover = 100.0 * 500  # 50,000
    brokerage = 20.0
    exchange = turnover * 0.0003503
    gst = (brokerage + exchange) * 0.18
    stt = turnover * 0.0015
    sebi = turnover * 0.000001
    expected = round(brokerage + exchange + gst + stt + sebi, 2)
    assert cost == expected


def test_buy_side_includes_stamp_duty_not_stt():
    cost = calculate_options_transaction_cost(price=100.0, quantity=500, transaction_type="BUY")
    turnover = 100.0 * 500
    brokerage = 20.0
    exchange = turnover * 0.0003503
    gst = (brokerage + exchange) * 0.18
    stamp_duty = turnover * 0.00003
    sebi = turnover * 0.000001
    expected = round(brokerage + exchange + gst + stamp_duty + sebi, 2)
    assert cost == expected


def test_zero_premium_still_charges_flat_brokerage():
    cost = calculate_options_transaction_cost(price=0.0, quantity=1, transaction_type="SELL")
    assert cost >= 20.0


def test_sell_side_costs_more_than_buy_side_for_same_premium():
    # STT (0.15%) on SELL is much larger than stamp duty (0.003%) on BUY.
    sell_cost = calculate_options_transaction_cost(100.0, 500, "SELL")
    buy_cost = calculate_options_transaction_cost(100.0, 500, "BUY")
    assert sell_cost > buy_cost


def test_case_insensitive_transaction_type():
    assert calculate_options_transaction_cost(50.0, 250, "sell") == \
        calculate_options_transaction_cost(50.0, 250, "SELL")
