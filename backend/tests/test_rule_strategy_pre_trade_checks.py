"""
tests/test_rule_strategy_pre_trade_checks.py — regression test for a
production-grade audit finding: RuleBasedStrategy.execute() (used by
EVERY custom strategy, paper and live) never called SEBI's pre-trade
price-band (±20% circuit) or freeze-quantity checks
(compliance/sebi_rules.py) — those only ever ran for the retired,
hand-written TenPercentOTMStrangle strategy via
run_all_pre_trade_checks(), which is hardcoded to its own fixed 2-leg
shape and can't be called from the generic N-leg builder. Fixed via
RuleBasedStrategy._run_pre_trade_checks(), called from execute() before
any leg is placed.

Same DataFeed + MockBroker test-double pattern as
test_partial_fill_auto_unwind.py — no network/DB.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backtest.data_feed import DataFeed
from broker.mock_broker import MockBroker
from compliance.sebi_rules import AuditTrail, ComplianceError, KillSwitch, OrderRateLimiter
from strategies.custom.rule_strategy import RuleBasedStrategy

EQUITY_KEY = "NSE_EQ|TEST_ISIN"
SPOT = 1300.0
STRIKE_STEP = 20
FAR_FUTURE_EXPIRY = "2099-12-31"


def _build_feed(strike: float, token: str) -> DataFeed:
    feed = DataFeed()
    feed.set_ltp(EQUITY_KEY, SPOT)
    feed.set_option_contracts(EQUITY_KEY, [FAR_FUTURE_EXPIRY])
    feed.set_option_chain(EQUITY_KEY, FAR_FUTURE_EXPIRY, [
        SimpleNamespace(strike_price=strike, call_options=SimpleNamespace(instrument_key=token), put_options=None),
    ])
    feed.set_ltp(token, 5.0)
    feed.set_time(__import__("datetime").datetime(2026, 1, 5, 10, 0, 0))
    return feed


def _build_strategy(feed: DataFeed, rules: dict) -> RuleBasedStrategy:
    broker = MockBroker(data_feed=feed, slippage_pct=0.0)
    with patch("utils.instrument_cache.InstrumentCache.resolve_equity_key", return_value=EQUITY_KEY), \
         patch.object(MockBroker, "get_lot_size", return_value=1):
        strategy = RuleBasedStrategy(
            broker=broker, audit=AuditTrail(audit_log_path="logs/test_audit_trail.log"),
            kill_switch=KillSwitch(), rate_limiter=OrderRateLimiter(max_per_second=10),
            symbol="TESTSTOCK", rules=rules, strike_step=STRIKE_STEP, product="NRML",
        )
    return strategy, broker


class TestPriceBandCheck:
    def test_strike_within_20_percent_of_spot_is_allowed(self):
        # 10% OTM — within the ±20% circuit band.
        strike = SPOT * 1.10
        token = "NSE_FO|CE_NEAR"
        feed = _build_feed(strike, token)
        rules = {"legs": [{"action": "SELL", "option_type": "CE", "strike_selection": {"mode": "OTM_PERCENT", "value": 10.0}, "lots": 1}]}
        strategy, _ = _build_strategy(feed, rules)
        result = strategy.execute()
        assert result["status"] in ("success", "dry_run")

    def test_strike_beyond_20_percent_of_spot_is_refused(self):
        # 30% OTM — breaches the ±20% circuit band; must be refused BEFORE any order is placed.
        strike = round(SPOT * 1.30 / STRIKE_STEP) * STRIKE_STEP
        token = "NSE_FO|CE_FAR"
        feed = _build_feed(strike, token)
        rules = {"legs": [{"action": "SELL", "option_type": "CE", "strike_selection": {"mode": "OTM_PERCENT", "value": 30.0}, "lots": 1}]}
        strategy, broker = _build_strategy(feed, rules)
        with pytest.raises(ComplianceError, match="circuit limit"):
            strategy.execute()
        assert broker.orders == []  # refused before any leg was placed — no partial fill to unwind


class TestFreezeQuantityCheckDoesNotBlockOrders:
    def test_freeze_quantity_check_runs_without_blocking_a_normal_order(self):
        """validate_order_quantity() only logs (Upstox's slice=True handles the actual split) — must never raise on its own."""
        strike = SPOT * 1.10
        token = "NSE_FO|CE_NEAR"
        feed = _build_feed(strike, token)
        rules = {"legs": [{"action": "SELL", "option_type": "CE", "strike_selection": {"mode": "OTM_PERCENT", "value": 10.0}, "lots": 1}]}
        strategy, _ = _build_strategy(feed, rules)
        with patch("compliance.sebi_rules._market_calendar.get_freeze_quantity", return_value=1):
            # freeze qty of 1 vs a real order quantity > 1 would normally warn-log, never raise.
            result = strategy.execute()
        assert result["status"] in ("success", "dry_run")
