"""
tests/test_equity_leg_support.py — EQUITY leg support added end-to-end
(rule_schema.py validation, rule_strategy.py execution, and
custom_strategy_scheduler.py's cycle-gating) as part of a production-grade
audit: the strategy builder was previously OPTIONS-only, so a small-cap
with no listed F&O contracts couldn't be traded at all even as a plain
cash BUY/SELL. Same DataFeed + MockBroker test-double pattern as
test_partial_fill_auto_unwind.py — no network/DB.
"""
from types import SimpleNamespace
from unittest.mock import patch

from backtest.data_feed import DataFeed
from broker.mock_broker import MockBroker
from compliance.sebi_rules import AuditTrail, KillSwitch, OrderRateLimiter
from strategies.custom.rule_schema import validate_rules
from strategies.custom.rule_strategy import RuleBasedStrategy

EQUITY_KEY = "NSE_EQ|TEST_ISIN"
SPOT = 1300.0


def _build_strategy(rules: dict, lot_size_should_be_called=False) -> tuple:
    feed = DataFeed()
    feed.set_ltp(EQUITY_KEY, SPOT)
    feed.set_time(__import__("datetime").datetime(2026, 1, 5, 10, 0, 0))
    broker = MockBroker(data_feed=feed, slippage_pct=0.0)
    with patch("utils.instrument_cache.InstrumentCache.resolve_equity_key", return_value=EQUITY_KEY), \
         patch.object(MockBroker, "get_lot_size", side_effect=AssertionError("get_lot_size should never be called for an all-EQUITY strategy")):
        strategy = RuleBasedStrategy(
            broker=broker, audit=AuditTrail(audit_log_path="logs/test_audit_trail.log"),
            kill_switch=KillSwitch(), rate_limiter=OrderRateLimiter(max_per_second=10),
            symbol="TESTSTOCK", rules=rules,
        )
    return strategy, broker


class TestRuleSchemaValidation:
    def test_valid_equity_leg(self):
        rules = {
            "legs": [{"instrument_type": "EQUITY", "action": "BUY", "lots": 10}],
            "entry": {"mode": "IMMEDIATE", "time": None},
            "exit": {"take_profit_pct": None, "stop_loss_pct": None, "exit_time": None, "exit_days_before_expiry": 0},
        }
        assert validate_rules(rules) == []

    def test_equity_leg_with_option_type_is_rejected(self):
        rules = {
            "legs": [{"instrument_type": "EQUITY", "action": "BUY", "option_type": "CE", "lots": 10}],
            "entry": {"mode": "IMMEDIATE", "time": None},
            "exit": {"take_profit_pct": None, "stop_loss_pct": None, "exit_time": None, "exit_days_before_expiry": 0},
        }
        errors = validate_rules(rules)
        assert any("option_type" in e for e in errors)

    def test_equity_leg_with_expiry_mode_is_rejected(self):
        rules = {
            "legs": [{"instrument_type": "EQUITY", "action": "BUY", "lots": 10, "expiry_mode": "WEEKLY"}],
            "entry": {"mode": "IMMEDIATE", "time": None},
            "exit": {"take_profit_pct": None, "stop_loss_pct": None, "exit_time": None, "exit_days_before_expiry": 0},
        }
        errors = validate_rules(rules)
        assert any("expiry" in e for e in errors)

    def test_omitted_instrument_type_still_defaults_to_option_backward_compat(self):
        rules = {
            "legs": [{"action": "SELL", "option_type": "CE", "strike_selection": {"mode": "ATM", "value": None}, "lots": 1}],
            "entry": {"mode": "IMMEDIATE", "time": None},
            "exit": {"take_profit_pct": None, "stop_loss_pct": None, "exit_time": None, "exit_days_before_expiry": 0},
        }
        assert validate_rules(rules) == []

    def test_duplicate_equity_legs_flagged(self):
        rules = {
            "legs": [
                {"instrument_type": "EQUITY", "action": "BUY", "lots": 10},
                {"instrument_type": "EQUITY", "action": "BUY", "lots": 5},
            ],
            "entry": {"mode": "IMMEDIATE", "time": None},
            "exit": {"take_profit_pct": None, "stop_loss_pct": None, "exit_time": None, "exit_days_before_expiry": 0},
        }
        errors = validate_rules(rules)
        assert any("identical" in e for e in errors)


class TestRuleStrategyExecution:
    def test_equity_only_strategy_never_needs_a_real_lot_size(self):
        """__init__ must not call broker.get_lot_size() at all when there are no OPTION legs."""
        rules = {"legs": [{"instrument_type": "EQUITY", "action": "BUY", "lots": 10}]}
        strategy, _broker = _build_strategy(rules)
        assert strategy.real_lot_size == 1
        assert strategy.strike_step is None

    def test_preview_resolves_an_equity_leg(self):
        rules = {"legs": [{"instrument_type": "EQUITY", "action": "BUY", "lots": 10}]}
        strategy, _ = _build_strategy(rules)
        result = strategy.preview()
        assert result["status"] == "success"
        leg = result["legs"][0]
        assert leg["instrument_type"] == "EQUITY"
        assert leg["option_type"] is None
        assert leg["strike"] is None
        assert leg["expiry"] is None
        assert leg["quantity"] == 10  # 10 lots * lot_size=1 -> 10 shares
        assert leg["instrument_token"] == EQUITY_KEY

    def test_execute_places_an_equity_order(self):
        rules = {"legs": [{"instrument_type": "EQUITY", "action": "BUY", "lots": 10}]}
        strategy, broker = _build_strategy(rules)
        result = strategy.execute()
        assert result["status"] == "success"
        assert result["legs"][0]["quantity"] == 10
        assert len(broker.orders) == 1

    def test_mixed_option_and_equity_legs_both_resolve(self):
        feed = DataFeed()
        feed.set_ltp(EQUITY_KEY, SPOT)
        ce_token = "NSE_FO|CE_TEST"
        feed.set_option_contracts(EQUITY_KEY, ["2099-12-31"])
        feed.set_option_chain(EQUITY_KEY, "2099-12-31", [
            SimpleNamespace(strike_price=SPOT, call_options=SimpleNamespace(instrument_key=ce_token), put_options=None),
        ])
        feed.set_ltp(ce_token, 5.0)
        feed.set_time(__import__("datetime").datetime(2026, 1, 5, 10, 0, 0))
        broker = MockBroker(data_feed=feed, slippage_pct=0.0)
        rules = {
            "legs": [
                {"instrument_type": "EQUITY", "action": "BUY", "lots": 10},
                {"action": "SELL", "option_type": "CE", "strike_selection": {"mode": "ATM", "value": None}, "lots": 1},
            ],
        }
        with patch("utils.instrument_cache.InstrumentCache.resolve_equity_key", return_value=EQUITY_KEY), \
             patch.object(MockBroker, "get_lot_size", return_value=50), \
             patch.object(MockBroker, "get_strike_step", return_value=20):
            strategy = RuleBasedStrategy(
                broker=broker, audit=AuditTrail(audit_log_path="logs/test_audit_trail.log"),
                kill_switch=KillSwitch(), rate_limiter=OrderRateLimiter(max_per_second=10),
                symbol="TESTSTOCK", rules=rules,
            )
            result = strategy.execute()
        assert result["status"] == "success"
        equity_leg, option_leg = result["legs"]
        assert equity_leg["instrument_type"] == "EQUITY" and equity_leg["quantity"] == 10  # NOT 10*50
        assert option_leg["instrument_type"] == "OPTION" and option_leg["quantity"] == 50  # 1 lot * real lot_size=50


class TestSchedulerCycleGating:
    def test_equity_legs_get_their_own_group_regardless_of_strategy_default(self):
        from api.custom_strategy_scheduler import _leg_groups
        rules = {
            "legs": [
                {"instrument_type": "EQUITY", "action": "BUY", "lots": 10},
                {"action": "SELL", "option_type": "CE", "strike_selection": {"mode": "ATM", "value": None}, "lots": 1},
            ],
            "expiry": {"mode": "MONTHLY"},
        }
        groups = _leg_groups(rules)
        assert groups["EQUITY"] == [0]
        assert groups["MONTHLY"] == [1]

    def test_resolve_current_expiry_equity_mode_returns_todays_date_without_a_broker_call(self):
        from datetime import date

        from api.custom_strategy_scheduler import _resolve_current_expiry
        assert _resolve_current_expiry(broker=None, symbol="TESTSTOCK", mode="EQUITY") == date.today().isoformat()
