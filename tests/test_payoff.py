"""
tests/test_payoff.py — pure-function tests for utils/payoff.py, including
compute_payoff_curve() added this session for the payoff-diagram chart.
No DB, no network — everything here is plain arithmetic.
"""
from automate.utils.payoff import compute_payoff, compute_payoff_curve, total_payoff


def _leg(action, option_type, strike, quantity, premium):
    return {"strike": strike, "option_type": option_type, "action": action, "quantity": quantity, "premium": premium}


class TestComputePayoffCurve:
    def test_empty_legs_returns_empty_curve(self):
        assert compute_payoff_curve([], 100.0) == []

    def test_curve_covers_the_expected_price_range(self):
        legs = [_leg("SELL", "CE", 1050, 50, 20.0), _leg("SELL", "PE", 950, 50, 20.0)]
        curve = compute_payoff_curve(legs, spot=1000.0)
        prices = [p["price"] for p in curve]
        assert min(prices) == 1000.0 * 0.85
        assert max(prices) == 1000.0 * 1.15
        assert prices == sorted(prices)  # strictly ascending, chartable as-is

    def test_strikes_are_included_as_exact_sample_points(self):
        legs = [_leg("SELL", "CE", 1053, 50, 20.0), _leg("SELL", "PE", 947, 50, 20.0)]
        curve = compute_payoff_curve(legs, spot=1000.0)
        prices = {p["price"] for p in curve}
        assert 1053.0 in prices
        assert 947.0 in prices

    def test_pnl_at_each_point_matches_total_payoff(self):
        legs = [_leg("BUY", "CE", 1000, 50, 15.0), _leg("BUY", "PE", 1000, 50, 15.0)]  # long straddle
        curve = compute_payoff_curve(legs, spot=1000.0)
        for point in curve:
            assert point["pnl"] == round(total_payoff(legs, point["price"]), 2)

    def test_short_strangle_shape_is_flat_topped_then_slopes_down(self):
        # SELL 950 PE + SELL 1050 CE, premium 20 each, qty 50 -> max profit
        # 2000 flat between the strikes, sloping down to loss outside them.
        legs = [_leg("SELL", "PE", 950, 50, 20.0), _leg("SELL", "CE", 1050, 50, 20.0)]
        curve = compute_payoff_curve(legs, spot=1000.0)
        mid = next(p for p in curve if p["price"] == 1000.0)
        assert mid["pnl"] == 2000.0
        far_low = curve[0]
        far_high = curve[-1]
        assert far_low["pnl"] < 2000.0
        assert far_high["pnl"] < 2000.0

    def test_zero_or_negative_spot_returns_empty(self):
        legs = [_leg("BUY", "CE", 100, 1, 5.0)]
        assert compute_payoff_curve(legs, spot=0) == []
        assert compute_payoff_curve(legs, spot=-50) == []


class TestComputePayoffBackwardCompat:
    """compute_payoff() itself (pre-existing) must be unaffected by the new function."""

    def test_short_strangle_max_profit_and_loss(self):
        legs = [_leg("SELL", "PE", 950, 50, 20.0), _leg("SELL", "CE", 1050, 50, 20.0)]
        result = compute_payoff(legs)
        assert result["max_profit"] == 2000.0
        assert result["max_loss"] is None  # unbounded on both sides for a short strangle
        assert result["net_premium"] == 2000.0
