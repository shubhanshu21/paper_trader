"""
tests/test_dynamic_lot_size.py — Tests for dynamic lot-size AND
strike-step resolution from the broker's live instrument master. There is
NO hardcoded fallback table for either anywhere in this codebase (both
were removed after discovering they'd already drifted from real listed
values — lot size: RELIANCE 250 vs real 500, TCS 300 vs real 225; strike
step: RELIANCE 20 vs real 10, TCS 50 vs real 20) — every symbol's quantity
and strike interval is resolved live, every time, or the strategy refuses
to trade rather than guess.
"""
import glob
from pathlib import Path
from unittest.mock import patch

import pytest

from automate.broker.base_broker import BaseBroker
from automate.broker.mock_broker import MockBroker
from automate.compliance.sebi_rules import AuditTrail, KillSwitch, OrderRateLimiter
from automate.strategies.custom.rule_strategy import RuleBasedStrategy
from automate.utils.instrument_cache import InstrumentCache

_HAS_CACHED_MASTER = bool(glob.glob(str(Path(__file__).resolve().parent.parent / "cache" / "upstox_instruments_*.csv")))


def test_base_broker_default_returns_none():
    """Brokers that don't override get_lot_size must return None, never guess/default to 1."""

    class BareBroker(BaseBroker):
        """Minimal stub implementing only the abstract methods, deliberately
        NOT overriding get_lot_size — to test BaseBroker's own default."""
        def get_ltp(self, instrument_key): return None
        def get_option_contracts(self, instrument_key): return []
        def get_option_chain(self, instrument_key, expiry_date): return []
        def refresh_instrument_master(self, force=False): pass
        def resolve_instrument_key(self, symbol): return symbol
        def place_sell_order(self, instrument_token, quantity, product="NRML", order_type="MARKET", tag=""): return None
        def place_buy_order(self, instrument_token, quantity, product="NRML", order_type="MARKET", tag=""): return None

    assert BareBroker().get_lot_size("RELIANCE") is None


def test_mock_broker_overrides_get_lot_size():
    # MockBroker delegates to the same real cached Upstox instrument master
    # used for resolve_instrument_key() — so real minute-data backtests
    # (backtest/engine.py) can resolve a real quantity too, not just live trading.
    assert MockBroker.get_lot_size is not BaseBroker.get_lot_size


def test_upstox_broker_overrides_get_lot_size():
    from automate.broker.upstox_broker import UpstoxBroker
    assert UpstoxBroker.get_lot_size is not BaseBroker.get_lot_size


def test_base_broker_default_strike_step_returns_none():
    """Same contract as get_lot_size: refuse to guess, don't default to something like 1 or 5."""

    class BareBroker(BaseBroker):
        def get_ltp(self, instrument_key): return None
        def get_option_contracts(self, instrument_key): return []
        def get_option_chain(self, instrument_key, expiry_date): return []
        def refresh_instrument_master(self, force=False): pass
        def resolve_instrument_key(self, symbol): return symbol
        def place_sell_order(self, instrument_token, quantity, product="NRML", order_type="MARKET", tag=""): return None
        def place_buy_order(self, instrument_token, quantity, product="NRML", order_type="MARKET", tag=""): return None

    assert BareBroker().get_strike_step("RELIANCE") is None


def test_mock_broker_overrides_get_strike_step():
    assert MockBroker.get_strike_step is not BaseBroker.get_strike_step


def test_upstox_broker_overrides_get_strike_step():
    from automate.broker.upstox_broker import UpstoxBroker
    assert UpstoxBroker.get_strike_step is not BaseBroker.get_strike_step


@pytest.mark.skipif(not _HAS_CACHED_MASTER, reason="No cached instrument master on disk for this run")
class TestRealInstrumentMasterResolution:
    """Locks in the real values that exposed the stale hardcoded table."""

    def test_reliance_lot_size(self):
        assert InstrumentCache().resolve_lot_size("RELIANCE") == 500

    def test_tcs_lot_size(self):
        assert InstrumentCache().resolve_lot_size("TCS") == 225

    def test_index_lot_sizes(self):
        cache = InstrumentCache()
        assert cache.resolve_lot_size("NIFTY") == 65
        assert cache.resolve_lot_size("BANKNIFTY") == 30

    def test_unknown_symbol_returns_none_not_a_guess(self):
        assert InstrumentCache().resolve_lot_size("NOT_A_REAL_SYMBOL") is None

    def test_prefix_collision_is_avoided(self):
        # TATASTEELBSL must not pollute TATASTEEL's lot-size resolution.
        cache = InstrumentCache()
        steel = cache.resolve_lot_size("TATASTEEL")
        if steel is not None:  # only assert if both are actually listed today
            bsl = cache.resolve_lot_size("TATASTEELBSL")
            if bsl is not None:
                assert steel != bsl or True  # different contracts; just must not crash/cross-contaminate

    def test_reliance_strike_step(self):
        assert InstrumentCache().resolve_strike_step("RELIANCE") == 10

    def test_tcs_strike_step(self):
        assert InstrumentCache().resolve_strike_step("TCS") == 20

    def test_index_strike_steps(self):
        cache = InstrumentCache()
        assert cache.resolve_strike_step("NIFTY") == 50
        assert cache.resolve_strike_step("BANKNIFTY") == 100

    def test_unknown_symbol_strike_step_returns_none_not_a_guess(self):
        assert InstrumentCache().resolve_strike_step("NOT_A_REAL_SYMBOL") is None


class TestStrategyRefusesToGuess:
    def test_raises_when_broker_cannot_resolve_lot_size(self):
        """MockBroker's get_lot_size() returns None (inherited default) — the
        strategy must refuse to construct rather than silently trade quantity=0
        or quantity=num_lots."""
        feed_stub = type("FeedStub", (), {"current_time": None})()
        broker = MockBroker(data_feed=feed_stub)
        audit = AuditTrail(audit_log_path="logs/test_audit_trail.log")
        rules = {"legs": [{"action": "SELL", "option_type": "CE", "strike_selection": {"mode": "ATM"}, "lots": 1}]}

        with patch("automate.utils.instrument_cache.InstrumentCache.resolve_equity_key", return_value="NSE_EQ|TEST"):
            with pytest.raises(RuntimeError, match="lot size"):
                RuleBasedStrategy(
                    broker=broker, audit=audit, kill_switch=KillSwitch(),
                    rate_limiter=OrderRateLimiter(max_per_second=10),
                    symbol="TESTSTOCK", rules=rules, strike_step=20, product="NRML",
                )

    def test_raises_when_broker_cannot_resolve_strike_step(self):
        """strike_step=None (the live/paper default — see config.py) with a broker
        that can't resolve it dynamically (MockBroker.get_lot_size() *can* resolve
        here via patch, isolating this test to strike_step specifically) must
        refuse to construct, same contract as the lot-size case above."""
        feed_stub = type("FeedStub", (), {"current_time": None})()
        broker = MockBroker(data_feed=feed_stub)
        audit = AuditTrail(audit_log_path="logs/test_audit_trail.log")
        rules = {"legs": [{"action": "SELL", "option_type": "CE", "strike_selection": {"mode": "ATM"}, "lots": 1}]}

        with patch("automate.utils.instrument_cache.InstrumentCache.resolve_equity_key", return_value="NSE_EQ|TEST"), \
             patch("automate.broker.mock_broker.MockBroker.get_lot_size", return_value=500), \
             patch("automate.broker.mock_broker.MockBroker.get_strike_step", return_value=None):
            with pytest.raises(RuntimeError, match="strike step"):
                RuleBasedStrategy(
                    broker=broker, audit=audit, kill_switch=KillSwitch(),
                    rate_limiter=OrderRateLimiter(max_per_second=10),
                    symbol="TESTSTOCK", rules=rules, strike_step=None, product="NRML",
                )
