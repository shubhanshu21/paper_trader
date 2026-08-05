"""
tests/test_trailing_stop_utils.py — direct unit tests for the shared
ratchet math in utils/trailing_stop.py, used by both
advanced_orders_scheduler.py (standalone trailing-stop orders) and
custom_strategy_scheduler.py/backtest/custom_engine.py (per-leg trailing
stops on a custom strategy). Pure functions, no DB/broker needed.
"""
from utils.trailing_stop import (
    advance_trailing_stop,
    exit_transaction_type,
    stop_triggered,
)


class TestAdvanceTrailingStopBuySide:
    def test_first_tick_seeds_highest_and_stop(self):
        highest, _lowest, stop, advanced = advance_trailing_stop("BUY", 100.0, 5.0, "points", None, None, None)
        assert highest == 100.0
        assert stop == 95.0
        assert advanced is True

    def test_new_high_ratchets_the_stop_up(self):
        highest, _lowest, stop, advanced = advance_trailing_stop("BUY", 110.0, 5.0, "points", 100.0, None, 95.0)
        assert highest == 110.0
        assert stop == 105.0
        assert advanced is True

    def test_a_dip_does_not_loosen_the_stop(self):
        highest, _lowest, stop, advanced = advance_trailing_stop("BUY", 102.0, 5.0, "points", 110.0, None, 105.0)
        assert highest == 110.0
        assert stop == 105.0
        assert advanced is False

    def test_percent_trail_type(self):
        _, _, stop, _ = advance_trailing_stop("BUY", 100.0, 10.0, "percent", None, None, None)
        assert stop == 90.0


class TestAdvanceTrailingStopSellSide:
    def test_first_tick_seeds_lowest_and_stop(self):
        _highest, lowest, stop, advanced = advance_trailing_stop("SELL", 100.0, 5.0, "points", None, None, None)
        assert lowest == 100.0
        assert stop == 105.0
        assert advanced is True

    def test_new_low_ratchets_the_stop_down(self):
        _highest, lowest, stop, advanced = advance_trailing_stop("SELL", 90.0, 5.0, "points", None, 100.0, 105.0)
        assert lowest == 90.0
        assert stop == 95.0
        assert advanced is True

    def test_a_rally_does_not_loosen_the_stop(self):
        _highest, lowest, stop, advanced = advance_trailing_stop("SELL", 98.0, 5.0, "points", None, 90.0, 95.0)
        assert lowest == 90.0
        assert stop == 95.0
        assert advanced is False


class TestExitTransactionType:
    def test_buy_side_exits_via_sell(self):
        assert exit_transaction_type("BUY") == "SELL"

    def test_sell_side_exits_via_buy(self):
        assert exit_transaction_type("SELL") == "BUY"


class TestStopTriggered:
    def test_buy_side_triggers_when_price_falls_to_or_through_stop(self):
        assert stop_triggered("BUY", 95.0, 95.0) is True
        assert stop_triggered("BUY", 94.9, 95.0) is True
        assert stop_triggered("BUY", 95.1, 95.0) is False

    def test_sell_side_triggers_when_price_rises_to_or_through_stop(self):
        assert stop_triggered("SELL", 105.0, 105.0) is True
        assert stop_triggered("SELL", 105.1, 105.0) is True
        assert stop_triggered("SELL", 104.9, 105.0) is False

    def test_no_stop_set_yet_never_triggers(self):
        assert stop_triggered("BUY", 50.0, None) is False
