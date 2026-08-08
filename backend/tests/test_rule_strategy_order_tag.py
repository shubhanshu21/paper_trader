"""
tests/test_rule_strategy_order_tag.py — unit tests for RuleBasedStrategy's
_order_tag() (strategies/custom/rule_strategy.py), which threads a real
CustomStrategy DB id into the broker order tag for live/paper orders so
they're traceable in Upstox's own order book back to the exact strategy
that placed them — see custom_strategy_scheduler.py's RuleBasedStrategy(...,
strategy_id=strategy.id) call. Called directly on a minimal stand-in
object (_order_tag only reads self.strategy_id/self.symbol) to avoid the
broker/DB setup a full RuleBasedStrategy construction needs.
"""
from types import SimpleNamespace

from strategies.custom.rule_strategy import RuleBasedStrategy

_order_tag = RuleBasedStrategy._order_tag


def _resolved(option_type="CE"):
    return {"option_type": option_type}


class TestOrderTagWithStrategyId:
    def test_entry_tag_includes_strategy_id(self):
        me = SimpleNamespace(strategy_id=4821, symbol="NIFTY")
        tag = _order_tag(me, "L", _resolved(), 0)
        assert tag == "S4821L0"

    def test_unwind_tag_includes_strategy_id(self):
        me = SimpleNamespace(strategy_id=4821, symbol="NIFTY")
        tag = _order_tag(me, "U", _resolved(), 2)
        assert tag == "S4821U2"

    def test_never_exceeds_upstox_16_char_limit(self):
        me = SimpleNamespace(strategy_id=999999999, symbol="NIFTY")
        tag = _order_tag(me, "L", _resolved(), 7)
        assert len(tag) <= 16

    def test_different_legs_get_distinct_tags(self):
        me = SimpleNamespace(strategy_id=100, symbol="NIFTY")
        assert _order_tag(me, "L", _resolved(), 0) != _order_tag(me, "L", _resolved(), 1)


class TestOrderTagWithoutStrategyId:
    """Backtest/CLI callers with no real DB strategy row — unchanged original format."""

    def test_falls_back_to_descriptive_format(self):
        me = SimpleNamespace(strategy_id=None, symbol="RELIANCE")
        tag = _order_tag(me, "L", _resolved("PE"), 1)
        assert tag == "CUSTOM_PE_RELIAN_1"

    def test_unwind_falls_back_to_descriptive_format(self):
        me = SimpleNamespace(strategy_id=None, symbol="RELIANCE")
        tag = _order_tag(me, "U", _resolved("CE"), 0)
        assert tag == "UNWIND_CE_RELIAN_0"
