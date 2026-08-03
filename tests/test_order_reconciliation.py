"""
tests/test_order_reconciliation.py — Tests for post-order reconciliation:
an order API call returning an order_id does NOT guarantee the exchange
accepted it (e.g. margin shortfall can reject it afterward). This must be
caught and treated the same as an outright placement failure, including
triggering auto-unwind for any companion leg that genuinely filled.
"""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.append(str(Path(__file__).resolve().parent.parent))

from tests.test_partial_fill_auto_unwind import _build_feed, _build_strategy


class TestReconciliation:
    def test_rejected_leg_is_reclassified_and_unwinds_companion(self):
        """Both legs appear to place successfully, but the broker's order-status
        lookup reveals the PE leg was actually rejected by the exchange — the
        CE leg (which really filled) must be bought back, not left naked."""
        feed = _build_feed(ce_has_ltp=True, pe_has_ltp=True)
        strategy, broker, _kill_switch = _build_strategy(feed)

        with patch.object(broker, "get_order_status") as mock_status:
            def status_side_effect(order_id):
                order = next((o for o in broker.orders if o["order_id"] == order_id), None)
                if order is None:
                    return None
                # Find which leg this was by matching against the strategy's known PE token.
                return "rejected" if "PE" in order.get("tag", "") else "complete"
            mock_status.side_effect = status_side_effect

            result = strategy.run()

        assert result["status"] == "failed"
        # CE genuinely filled then got auto-unwound; PE was reclassified as
        # failed/rejected — no unwind attempted for it, since a rejected
        # order never actually executed in reality (nothing to buy back).
        buys = [o for o in broker.orders if o["transaction_type"] == "BUY"]
        assert len(buys) == 1
        assert buys[0]["instrument_token"] == "NSE_FO|CE_TEST"
        assert broker.positions.get("NSE_FO|CE_TEST", 0) == 0, "CE leg left naked after reconciliation + unwind"

    def test_reconciliation_is_noop_when_broker_doesnt_support_it(self):
        """MockBroker's default get_order_status() returns None — reconciliation
        must not interfere with the normal success path in that case."""
        feed = _build_feed(ce_has_ltp=True, pe_has_ltp=True)
        strategy, _broker, kill_switch = _build_strategy(feed)

        result = strategy.run()

        assert result["status"] in ("success", "dry_run")
        assert kill_switch.is_active() is False

    def test_reconciliation_skipped_in_dry_run(self):
        """dry_run brokers never place real orders, so there's nothing to reconcile."""
        feed = _build_feed(ce_has_ltp=True, pe_has_ltp=True)
        strategy, broker, _kill_switch = _build_strategy(feed)
        broker.dry_run = True

        with patch.object(broker, "get_order_status") as mock_status:
            strategy.run()
            mock_status.assert_not_called()
