"""
tests/test_position_reconciliation.py — utils/position_reconciliation.py,
the safety net added for a real gap found in a production-grade audit:
custom_strategy_scheduler.py places a real broker order BEFORE
committing the matching DB row, so a process crash in that window can
leave the broker holding a real position this app has no record of (or
the mirror case: a DB row still marked OPEN after the broker leg was
actually closed). This module detects — never auto-corrects — that
drift. Pure functions, fully hermetic.
"""
from automate.utils.position_reconciliation import (
    compute_db_net_quantity, diff_positions, reconcile_live_positions,
)


class TestComputeDbNetQuantity:
    def test_single_sell_leg_is_negative(self):
        legs = [{"instrument_key": "A", "transaction_type": "SELL", "quantity": 50}]
        assert compute_db_net_quantity(legs) == {"A": -50}

    def test_single_buy_leg_is_positive(self):
        legs = [{"instrument_key": "A", "transaction_type": "BUY", "quantity": 50}]
        assert compute_db_net_quantity(legs) == {"A": 50}

    def test_multiple_legs_on_the_same_instrument_net_together(self):
        legs = [
            {"instrument_key": "A", "transaction_type": "SELL", "quantity": 50},
            {"instrument_key": "A", "transaction_type": "BUY", "quantity": 20},
        ]
        assert compute_db_net_quantity(legs) == {"A": -30}

    def test_no_legs_is_empty(self):
        assert compute_db_net_quantity([]) == {}


class TestDiffPositions:
    def test_matching_positions_produce_no_mismatch(self):
        assert diff_positions({"A": -50}, {"A": -50}) == []

    def test_orphaned_broker_position_not_in_db_is_flagged(self):
        """The exact crash scenario: order placed at broker, DB commit never happened."""
        mismatches = diff_positions(db_net={}, broker_net={"A": -50})
        assert mismatches == [{"instrument_key": "A", "db_quantity": 0, "broker_quantity": -50}]

    def test_db_open_but_broker_flat_is_flagged(self):
        """Mirror scenario: DB still thinks it's open, broker shows flat (closed outside the app, or exit-crash)."""
        mismatches = diff_positions(db_net={"A": -50}, broker_net={})
        assert mismatches == [{"instrument_key": "A", "db_quantity": -50, "broker_quantity": 0}]

    def test_quantity_mismatch_on_the_same_instrument_is_flagged(self):
        mismatches = diff_positions(db_net={"A": -50}, broker_net={"A": -30})
        assert mismatches == [{"instrument_key": "A", "db_quantity": -50, "broker_quantity": -30}]

    def test_unrelated_matching_instruments_are_not_flagged(self):
        mismatches = diff_positions(db_net={"A": -50, "B": 100}, broker_net={"A": -50, "B": 100})
        assert mismatches == []

    def test_results_sorted_by_instrument_key_for_stable_output(self):
        mismatches = diff_positions(db_net={"Z": 1, "A": 1}, broker_net={})
        assert [m["instrument_key"] for m in mismatches] == ["A", "Z"]


class TestReconcileLivePositions:
    def test_broker_net_none_means_could_not_check(self):
        """The broker call itself failed — must be distinguishable from 'confirmed everything matches'."""
        legs = [{"instrument_key": "A", "transaction_type": "SELL", "quantity": 50}]
        assert reconcile_live_positions(legs, broker_net=None) is None

    def test_end_to_end_matching(self):
        legs = [{"instrument_key": "A", "transaction_type": "SELL", "quantity": 50}]
        assert reconcile_live_positions(legs, broker_net={"A": -50}) == []

    def test_end_to_end_mismatch(self):
        legs = [{"instrument_key": "A", "transaction_type": "SELL", "quantity": 50}]
        result = reconcile_live_positions(legs, broker_net={})
        assert result == [{"instrument_key": "A", "db_quantity": -50, "broker_quantity": 0}]
