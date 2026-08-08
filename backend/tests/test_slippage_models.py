"""
tests/test_slippage_models.py — pure-function tests for
utils/slippage_models.py, plus MockBroker/PaperBroker wiring smoke tests
confirming a pluggable model actually drives execution_price (not just the
default flat fraction).
"""
from utils.slippage_models import (
    as_model,
    fixed_pct,
    random_band_pct,
    volume_scaled_pct,
)


class TestFixedPct:
    def test_same_pct_regardless_of_size_or_side(self):
        model = fixed_pct(0.002)
        assert model(100.0, 1, True) == 0.002
        assert model(100.0, 10_000, False) == 0.002


class TestVolumeScaledPct:
    def test_at_or_below_threshold_behaves_like_fixed(self):
        model = volume_scaled_pct(base_pct=0.001, quantity_threshold=100)
        assert model(100.0, 50, True) == 0.001
        assert model(100.0, 100, True) == 0.001

    def test_scales_up_past_threshold(self):
        model = volume_scaled_pct(base_pct=0.001, quantity_threshold=100, scale_per_threshold=0.5)
        # 3x the threshold -> +0.5 +0.5 = 2x base_pct
        assert model(100.0, 300, True) == 0.001 * 2.0

    def test_larger_orders_always_slip_at_least_as_much(self):
        model = volume_scaled_pct(base_pct=0.001, quantity_threshold=50)
        small = model(100.0, 60, True)
        large = model(100.0, 600, True)
        assert large > small


class TestRandomBandPct:
    def test_stays_within_band(self):
        model = random_band_pct(0.0005, 0.003, seed=1)
        for _ in range(200):
            pct = model(100.0, 1, True)
            assert 0.0005 <= pct <= 0.003

    def test_deterministic_given_seed(self):
        a = random_band_pct(0.0005, 0.003, seed=42)
        b = random_band_pct(0.0005, 0.003, seed=42)
        seq_a = [a(100.0, 1, True) for _ in range(20)]
        seq_b = [b(100.0, 1, True) for _ in range(20)]
        assert seq_a == seq_b


class TestAsModel:
    def test_wraps_bare_float(self):
        model = as_model(0.0015)
        assert model(100.0, 1, True) == 0.0015

    def test_passes_through_callable_unchanged(self):
        custom = fixed_pct(0.01)
        assert as_model(custom) is custom


class TestMockBrokerSlippageModel:
    def test_default_float_still_works(self):
        from broker.mock_broker import MockBroker

        class _StubFeed:
            current_time = "2026-01-01T09:20:00"
            def get_ltp(self, key):
                return 100.0

        broker = MockBroker(data_feed=_StubFeed(), slippage_pct=0.01)
        order_id = broker._place_order("BUY", "NSE_FO|TEST", 1)
        assert broker.get_fill_price(order_id) == 101.0

    def test_pluggable_model_drives_execution_price(self):
        from broker.mock_broker import MockBroker

        class _StubFeed:
            current_time = "2026-01-01T09:20:00"
            def get_ltp(self, key):
                return 100.0

        broker = MockBroker(data_feed=_StubFeed(), slippage_pct=fixed_pct(0.05))
        assert broker.slippage_pct is None  # flat float attr unset when a model is used
        order_id = broker._place_order("SELL", "NSE_FO|TEST", 1)
        assert broker.get_fill_price(order_id) == 95.0
