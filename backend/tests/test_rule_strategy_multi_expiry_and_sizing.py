"""
tests/test_rule_strategy_multi_expiry_and_sizing.py — Phase 3 coverage for
strategies/custom/rule_strategy.py: per-leg multi-expiry resolution
(calendar spreads, rule_schema.py's leg.expiry_mode) and risk-based
position sizing (leg.sizing.mode == "RISK_PCT"). Same test-double pattern
as tests/test_partial_fill_auto_unwind.py — DataFeed + MockBroker, no
network/DB.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backtest.data_feed import DataFeed
from broker.mock_broker import MockBroker
from compliance.sebi_rules import AuditTrail, ComplianceError, KillSwitch, OrderRateLimiter
from strategies.custom.rule_strategy import RuleBasedStrategy

EQUITY_KEY = "NSE_EQ|TEST_ISIN"
SPOT = 20000.0
STRIKE_STEP = 100
WEEKLY_EXPIRY = "2026-01-08"
MONTHLY_EXPIRY = "2026-01-29"


def _build_feed() -> DataFeed:
    weekly_ce_token, monthly_ce_token = "NSE_FO|CE_WEEKLY", "NSE_FO|CE_MONTHLY"
    feed = DataFeed()
    feed.set_ltp(EQUITY_KEY, SPOT)
    feed.set_option_contracts(EQUITY_KEY, [WEEKLY_EXPIRY, MONTHLY_EXPIRY])
    feed.set_option_chain(EQUITY_KEY, WEEKLY_EXPIRY, [
        SimpleNamespace(strike_price=SPOT, call_options=SimpleNamespace(instrument_key=weekly_ce_token), put_options=None),
    ])
    feed.set_option_chain(EQUITY_KEY, MONTHLY_EXPIRY, [
        SimpleNamespace(strike_price=SPOT, call_options=SimpleNamespace(instrument_key=monthly_ce_token), put_options=None),
    ])
    feed.set_ltp(weekly_ce_token, 50.0)
    feed.set_ltp(monthly_ce_token, 120.0)
    feed.set_time(__import__("datetime").datetime(2026, 1, 5, 10, 0, 0))
    return feed, weekly_ce_token, monthly_ce_token


def _build_strategy(feed, rules, user_id=None) -> RuleBasedStrategy:
    broker = MockBroker(data_feed=feed, slippage_pct=0.0)
    with patch("utils.instrument_cache.InstrumentCache.resolve_equity_key", return_value=EQUITY_KEY), \
         patch.object(MockBroker, "get_lot_size", return_value=50):
        strategy = RuleBasedStrategy(
            broker=broker, audit=AuditTrail(audit_log_path="logs/test_audit_trail.log"),
            kill_switch=KillSwitch(), rate_limiter=OrderRateLimiter(max_per_second=10),
            symbol="NIFTY", rules=rules, strike_step=STRIKE_STEP, product="NRML", user_id=user_id,
        )
    return strategy, broker


class TestCalendarSpreadMultiExpiry:
    """A near-week SELL leg + a far-month BUY leg, on independent expiry_mode overrides."""

    def _rules(self):
        return {
            "legs": [
                {"action": "SELL", "option_type": "CE", "strike_selection": {"mode": "ATM", "value": None}, "lots": 1, "expiry_mode": "WEEKLY"},
                {"action": "BUY", "option_type": "CE", "strike_selection": {"mode": "ATM", "value": None}, "lots": 1, "expiry_mode": "MONTHLY"},
            ],
        }

    def test_preview_resolves_each_leg_to_its_own_expiry(self):
        feed, weekly_token, monthly_token = _build_feed()
        strategy, _ = _build_strategy(feed, self._rules())
        result = strategy.preview()
        assert result["status"] == "success"
        assert result["expiries"] == {"WEEKLY": WEEKLY_EXPIRY, "MONTHLY": MONTHLY_EXPIRY}
        near_leg, far_leg = result["legs"]
        assert near_leg["expiry"] == WEEKLY_EXPIRY and near_leg["instrument_token"] == weekly_token
        assert far_leg["expiry"] == MONTHLY_EXPIRY and far_leg["instrument_token"] == monthly_token

    def test_execute_fills_both_legs_at_their_own_expiry(self):
        feed, _weekly_token, _monthly_token = _build_feed()
        strategy, _broker = _build_strategy(feed, self._rules())
        result = strategy.execute()
        assert result["status"] == "success"
        assert len(result["legs"]) == 2
        assert result["legs"][0]["expiry"] == WEEKLY_EXPIRY
        assert result["legs"][1]["expiry"] == MONTHLY_EXPIRY
        assert result["leg_indices"] == [0, 1]

    def test_single_expiry_strategy_resolves_one_shared_expiry(self):
        """Backward-compat guarantee: no expiry_mode override anywhere -> exactly one resolved expiry for all legs."""
        feed, _weekly_token, _ = _build_feed()
        rules = {
            "legs": [
                {"action": "SELL", "option_type": "CE", "strike_selection": {"mode": "ATM", "value": None}, "lots": 1},
            ],
        }
        strategy, _ = _build_strategy(feed, rules)
        result = strategy.preview()
        assert set(result["expiries"].keys()) == {"WEEKLY"}
        assert result["legs"][0]["expiry"] == WEEKLY_EXPIRY
        assert result["expiry"] == WEEKLY_EXPIRY  # backward-compat single "expiry" field


class TestRunLegIndicesFilter:
    def test_leg_indices_restricts_execution_to_a_subset(self):
        feed, _weekly_token, _monthly_token = _build_feed()
        strategy, _broker = _build_strategy(feed, TestCalendarSpreadMultiExpiry()._rules())
        result = strategy.run(leg_indices=[1])
        assert result["status"] == "success"
        assert result["leg_indices"] == [1]
        assert len(result["legs"]) == 1
        assert result["legs"][0]["expiry"] == MONTHLY_EXPIRY


class TestRiskBasedSizing:
    def _rules(self, risk_pct):
        return {
            "legs": [
                {"action": "SELL", "option_type": "CE", "strike_selection": {"mode": "ATM", "value": None}, "lots": 1,
                 "sizing": {"mode": "RISK_PCT", "risk_pct": risk_pct}},
            ],
        }

    def test_sizes_lots_from_available_capital_and_margin_estimate(self):
        feed, _weekly_token, _ = _build_feed()
        strategy, _ = _build_strategy(feed, self._rules(risk_pct=50.0), user_id=1)
        with patch("utils.wallet.get_wallet_summary", return_value={"available_balance": 1_000_000.0}), \
             patch("utils.margin.estimate_margin_blocked", return_value=100_000.0):
            result = strategy.preview()
        assert result["status"] == "success"
        # budget = 500,000; per-lot margin = 100,000 -> 5 lots * 50 (lot size) = 250 quantity
        assert result["legs"][0]["quantity"] == 250

    def test_prefers_real_broker_margin_over_the_flat_estimate(self):
        """resolve_required_margin() tries broker.get_required_margin() first — a real per-lot figure from the broker must win over the flat-rate guess."""
        feed, _weekly_token, _ = _build_feed()
        strategy, broker = _build_strategy(feed, self._rules(risk_pct=50.0), user_id=1)
        with patch("utils.wallet.get_wallet_summary", return_value={"available_balance": 1_000_000.0}), \
             patch.object(type(broker), "get_required_margin", return_value=250_000.0), \
             patch("utils.margin.estimate_margin_blocked", return_value=999_999.0) as flat_estimate:
            result = strategy.preview()
        assert result["status"] == "success"
        # budget = 500,000; REAL per-lot margin = 250,000 -> 2 lots * 50 = 100 quantity (not the flat estimate's number)
        assert result["legs"][0]["quantity"] == 100
        flat_estimate.assert_not_called()

    def test_refuses_entry_rather_than_undersizing_below_one_lot(self):
        feed, _weekly_token, _ = _build_feed()
        strategy, _ = _build_strategy(feed, self._rules(risk_pct=1.0), user_id=1)
        with (
            patch("utils.wallet.get_wallet_summary", return_value={"available_balance": 10_000.0}),
            patch("utils.margin.estimate_margin_blocked", return_value=100_000.0),
            pytest.raises(ComplianceError),
        ):
            strategy.preview()

    def test_requires_a_real_user_id_for_risk_pct_sizing(self):
        feed, _weekly_token, _ = _build_feed()
        strategy, _ = _build_strategy(feed, self._rules(risk_pct=50.0), user_id=None)
        with pytest.raises(RuntimeError):
            strategy.preview()

    def test_lots_mode_is_unaffected_default(self):
        feed, _weekly_token, _ = _build_feed()
        rules = {
            "legs": [
                {"action": "SELL", "option_type": "CE", "strike_selection": {"mode": "ATM", "value": None}, "lots": 2},
            ],
        }
        strategy, _ = _build_strategy(feed, rules)
        result = strategy.preview()
        assert result["legs"][0]["quantity"] == 100  # 2 lots * 50 lot size, no wallet/margin calls needed
