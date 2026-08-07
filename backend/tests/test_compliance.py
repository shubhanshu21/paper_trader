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

from compliance.sebi_rules import (
    KillSwitch,
    OrderRateLimiter,
    assert_kill_switch_not_active,
    get_global_kill_switch,
    validate_price_band,
)


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

    def test_status_reports_reason_and_timestamp_once_active(self):
        ks = KillSwitch()
        assert ks.status() == {"active": False, "reason": None, "activated_at": None}
        ks.activate(reason="daily drawdown breached")
        status = ks.status()
        assert status["active"] is True
        assert status["reason"] == "daily drawdown breached"
        assert status["activated_at"] is not None

    def test_reset_clears_reason_and_timestamp(self):
        ks = KillSwitch()
        ks.activate(reason="test")
        ks.reset()
        assert ks.status() == {"active": False, "reason": None, "activated_at": None}


class TestGlobalKillSwitch:
    def test_returns_the_same_instance_every_call(self):
        # The whole point: every one of the 11 strategy engines that used
        # to build its OWN KillSwitch() now shares this one, so activating
        # it from any single place actually halts every engine's order
        # flow, not just the one that tripped it.
        assert get_global_kill_switch() is get_global_kill_switch()

    def test_activating_the_global_switch_is_visible_everywhere_it_is_referenced(self):
        ks = get_global_kill_switch()
        was_active = ks.is_active()
        try:
            ks.activate(reason="test — visible everywhere")
            assert get_global_kill_switch().is_active() is True
        finally:
            ks.reset()
            assert was_active is False  # sanity: this test didn't inherit an already-active switch from another test


class TestAssertKillSwitchNotActive:
    def test_none_is_always_a_pass_through(self):
        assert_kill_switch_not_active(None)  # must not raise — read-only preview engines pass kill_switch=None

    def test_inactive_switch_does_not_raise(self):
        assert_kill_switch_not_active(KillSwitch())

    def test_active_switch_raises(self):
        ks = KillSwitch()
        ks.activate(reason="test")
        with pytest.raises(RuntimeError, match="KILL SWITCH IS ACTIVE"):
            assert_kill_switch_not_active(ks)


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
