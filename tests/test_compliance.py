"""
tests/test_compliance.py — Tests for the pure-logic SEBI compliance gates
(KillSwitch, OrderRateLimiter, price band check). Deliberately does NOT
test assert_market_is_open()/MarketCalendar here — those hit a live NSE
API on first use (only avoided today because a same-year holiday cache
happens to already exist on disk), so testing them would make this suite
network-dependent and flaky. Covered instead by backtest/engine.py's
`get_current_time()` simulated-time path, exercised manually each session.
"""
import pytest

from automate.compliance.sebi_rules import KillSwitch, OrderRateLimiter, validate_price_band


class TestKillSwitch:
    def test_starts_inactive(self):
        assert KillSwitch().is_active() is False

    def test_activate_sets_active(self):
        ks = KillSwitch()
        ks.activate(reason="test")
        assert ks.is_active() is True

    def test_reset_clears_active(self):
        ks = KillSwitch()
        ks.activate(reason="test")
        ks.reset()
        assert ks.is_active() is False

    def test_activate_is_idempotent(self):
        ks = KillSwitch()
        ks.activate(reason="first")
        ks.activate(reason="second")  # must not raise / must stay active
        assert ks.is_active() is True


class TestOrderRateLimiter:
    def test_allows_orders_under_limit_without_blocking(self):
        import time
        limiter = OrderRateLimiter(max_per_second=10)
        start = time.monotonic()
        for _ in range(5):
            limiter.acquire()
        elapsed = time.monotonic() - start
        assert elapsed < 0.5  # should not have needed to sleep at all

    def test_blocks_once_limit_exceeded_within_one_second(self):
        import time
        limiter = OrderRateLimiter(max_per_second=3)
        start = time.monotonic()
        for _ in range(4):  # 4th call must wait for the sliding window
            limiter.acquire()
        elapsed = time.monotonic() - start
        assert elapsed > 0.05  # some wait was enforced


class TestValidatePriceBand:
    def test_passes_within_twenty_percent(self):
        validate_price_band(strike_price=1100, spot_price=1000.0)  # 10% — must not raise

    def test_rejects_beyond_twenty_percent(self):
        with pytest.raises(ValueError):
            validate_price_band(strike_price=1300, spot_price=1000.0)  # 30%
