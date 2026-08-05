"""
tests/test_custom_backtest_calendar_and_leg_exit.py — Phase 3 coverage for
backtest/custom_engine.py::CustomRuleBacktestEngine's per-leg exit walk and
multi-expiry (calendar-spread) cycle discovery, added alongside the same
feature in custom_strategy_scheduler.py/rule_strategy.py this session.

Same __new__()-bypass test-double pattern as test_custom_backtest_engine.py
for the DB-free cases; the per-leg-exit-walk and calendar-spread cases need
a REAL session (the shared automate_test MySQL schema — see
tests/conftest.py) because _run_one_cycle's day-by-day walk queries
fno_bhavcopy directly via `text()` SQL.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backtest.custom_engine import CustomRuleBacktestEngine
from backtest.data_feed import DataFeed
from broker.mock_broker import MockBroker
from compliance.sebi_rules import AuditTrail, OrderRateLimiter
from db.models import FnoBhavcopy
from utils.option_utils import calculate_strangle_strikes

EQUITY_KEY = "NSE_EQ|TEST_ISIN"
SPOT = 1300.0
STRIKE_STEP = 20
EXPIRY = "2099-12-31"
SLIPPAGE_PCT = 0.02


class _TestFeed(DataFeed):
    def get_volume(self, instrument_key: str) -> int:
        return 1


class _DayAwareFeed(_TestFeed):
    """
    Like _TestFeed, but one token's price can vary by day (via
    `by_day[instrument_key] = {date_str: price}`) — needed to simulate a
    price MOVE that crosses a per-leg TP/SL/trailing trigger on a specific
    day, which a fully static feed can't represent (entry and every later
    check would see the identical price).
    """
    def __init__(self):
        super().__init__()
        self.by_day: dict = {}

    def get_ltp(self, instrument_key: str) -> float | None:
        day_prices = self.by_day.get(instrument_key)
        if day_prices and self.current_time is not None:
            day = self.current_time.date().isoformat()
            if day in day_prices:
                return day_prices[day]
        return super().get_ltp(instrument_key)


def _build_feed(ce_price: float, pe_price: float, ce_token: str, pe_token: str) -> _TestFeed:
    call_strike, put_strike = calculate_strangle_strikes(SPOT, 0.10, STRIKE_STEP)
    feed = _TestFeed()
    feed.set_ltp(EQUITY_KEY, SPOT)
    feed.set_option_contracts(EQUITY_KEY, [EXPIRY])
    feed.set_option_chain(EQUITY_KEY, EXPIRY, [
        SimpleNamespace(strike_price=call_strike, call_options=SimpleNamespace(instrument_key=ce_token), put_options=None),
        SimpleNamespace(strike_price=put_strike, call_options=None, put_options=SimpleNamespace(instrument_key=pe_token)),
    ])
    feed.set_ltp(ce_token, ce_price)
    feed.set_ltp(pe_token, pe_price)
    return feed


def _build_engine(feed: _TestFeed, rules: dict, session=None) -> CustomRuleBacktestEngine:
    engine = CustomRuleBacktestEngine.__new__(CustomRuleBacktestEngine)
    engine.symbol = "TESTSTOCK"
    engine.rules = rules
    engine.strike_step = STRIKE_STEP
    engine.product = "NRML"
    engine.option_instrument = "OPTSTK"
    engine.future_instrument = "FUTSTK"
    engine.session = session
    engine.equity_key = EQUITY_KEY
    engine.feed = feed
    engine.broker = MockBroker(data_feed=feed, slippage_pct=SLIPPAGE_PCT)
    engine.audit = AuditTrail(audit_log_path="logs/test_audit_trail.log")
    engine.rate_limiter = OrderRateLimiter(max_per_second=10)
    engine.charge_rates = None
    return engine


_CYCLE = {"entry_date": "2026-01-05", "expiry": EXPIRY, "exit_date": "2026-01-29"}


class TestDrivingExpiryMode:
    def test_single_expiry_strategy_uses_its_own_mode(self):
        rules = {"legs": [{"action": "SELL", "option_type": "CE"}], "expiry": {"mode": "MONTHLY"}}
        engine = CustomRuleBacktestEngine.__new__(CustomRuleBacktestEngine)
        engine.rules = rules
        assert engine._driving_expiry_mode() == "MONTHLY"

    def test_calendar_spread_prefers_weekly_as_the_more_frequent_stream(self):
        rules = {
            "legs": [
                {"action": "SELL", "option_type": "CE", "expiry_mode": "WEEKLY"},
                {"action": "BUY", "option_type": "CE", "expiry_mode": "MONTHLY"},
            ],
            "expiry": {"mode": "MONTHLY"},
        }
        engine = CustomRuleBacktestEngine.__new__(CustomRuleBacktestEngine)
        engine.rules = rules
        assert engine._driving_expiry_mode() == "WEEKLY"


class TestPerLegIndependentExit:
    """A leg with its own exit config closes on its own trigger, independent of a sibling leg with no config."""

    def _rules(self, ce_take_profit_pct):
        return {
            "legs": [
                {"action": "SELL", "option_type": "CE", "strike_selection": {"mode": "OTM_PERCENT", "value": 10.0}, "lots": 1,
                 "exit": {"take_profit_pct": ce_take_profit_pct}},
                {"action": "SELL", "option_type": "PE", "strike_selection": {"mode": "OTM_PERCENT", "value": 10.0}, "lots": 1},
            ],
        }

    def test_leg_with_own_take_profit_exits_early_sibling_rides_to_expiry(self, db_session):
        ce_token, pe_token = "NSE_FO|CE_TEST", "NSE_FO|PE_TEST"
        # Entry premium 10.0, then a big drop to 2.0 on the checked day — a
        # SELL leg profits as price falls, easily clearing a 50%
        # take-profit target (only representable with a day-aware feed;
        # a fully static feed would show the identical price at entry and
        # at every later check, so nothing would ever trigger).
        feed = _DayAwareFeed()
        feed.set_ltp(EQUITY_KEY, SPOT)
        call_strike, put_strike = calculate_strangle_strikes(SPOT, 0.10, STRIKE_STEP)
        feed.set_option_contracts(EQUITY_KEY, [EXPIRY])
        feed.set_option_chain(EQUITY_KEY, EXPIRY, [
            SimpleNamespace(strike_price=call_strike, call_options=SimpleNamespace(instrument_key=ce_token), put_options=None),
            SimpleNamespace(strike_price=put_strike, call_options=None, put_options=SimpleNamespace(instrument_key=pe_token)),
        ])
        feed.set_ltp(ce_token, 10.0)
        feed.set_ltp(pe_token, 8.0)
        feed.by_day[ce_token] = {"2026-01-06": 2.0}
        session = db_session
        session.add(FnoBhavcopy(symbol="TESTSTOCK", instrument="OPTSTK", expiry_dt=EXPIRY,
                                 trade_date="2026-01-06", close=2.0))
        session.commit()
        engine = _build_engine(feed, self._rules(50.0), session=session)
        with patch("utils.instrument_cache.InstrumentCache.resolve_equity_key", return_value=EQUITY_KEY), \
             patch.object(MockBroker, "get_lot_size", return_value=1):
            row = engine._run_one_cycle(dict(_CYCLE))

        assert row is not None
        ce_leg, pe_leg = row["legs"][0], row["legs"][1]
        assert ce_leg["option_type"] == "CE" and pe_leg["option_type"] == "PE"
        assert ce_leg["exit_reason"] == "TAKE_PROFIT"
        assert ce_leg["exit_date"] == "2026-01-06"
        assert pe_leg["exit_reason"] == "EXPIRY"
        assert pe_leg["exit_date"] == _CYCLE["exit_date"]
        # Cycle-level fields summarize a MIXED outcome since legs closed on different days/reasons.
        assert row["exit_reason"] == "MIXED"
        assert row["exit_date"] == _CYCLE["exit_date"]

    def test_no_own_exit_config_never_touches_the_session(self):
        """Backward-compat guarantee: a strategy with no per-leg or strategy-level exit config never queries the DB."""
        ce_token, pe_token = "NSE_FO|CE_TEST", "NSE_FO|PE_TEST"
        feed = _build_feed(ce_price=5.0, pe_price=4.0, ce_token=ce_token, pe_token=pe_token)
        rules = {
            "legs": [
                {"action": "SELL", "option_type": "CE", "strike_selection": {"mode": "OTM_PERCENT", "value": 10.0}, "lots": 1},
                {"action": "SELL", "option_type": "PE", "strike_selection": {"mode": "OTM_PERCENT", "value": 10.0}, "lots": 1},
            ],
        }
        engine = _build_engine(feed, rules, session=None)  # None: any DB touch would AttributeError
        with patch("utils.instrument_cache.InstrumentCache.resolve_equity_key", return_value=EQUITY_KEY), \
             patch.object(MockBroker, "get_lot_size", return_value=1):
            row = engine._run_one_cycle(dict(_CYCLE))
        assert row is not None
        assert row["exit_reason"] == "EXPIRY"
        assert row["exit_date"] == _CYCLE["exit_date"]


class TestTrailingStopLegExit:
    def test_trailing_stop_leg_exits_when_price_reverses_past_the_ratchet(self, db_session):
        ce_token, pe_token = "NSE_FO|CE_TEST", "NSE_FO|PE_TEST"
        # SELL CE trailing stop seeded at entry_price=20.0 -> stop=21.0
        # (trail_amount=1 point). A rise to 22.0 on the checked day crosses
        # back above the stop -> BUY-to-close trigger.
        feed = _DayAwareFeed()
        feed.set_ltp(EQUITY_KEY, SPOT)
        call_strike, put_strike = calculate_strangle_strikes(SPOT, 0.10, STRIKE_STEP)
        feed.set_option_contracts(EQUITY_KEY, [EXPIRY])
        feed.set_option_chain(EQUITY_KEY, EXPIRY, [
            SimpleNamespace(strike_price=call_strike, call_options=SimpleNamespace(instrument_key=ce_token), put_options=None),
            SimpleNamespace(strike_price=put_strike, call_options=None, put_options=SimpleNamespace(instrument_key=pe_token)),
        ])
        feed.set_ltp(ce_token, 20.0)
        feed.set_ltp(pe_token, 8.0)
        feed.by_day[ce_token] = {"2026-01-06": 22.0}
        session = db_session
        session.add(FnoBhavcopy(symbol="TESTSTOCK", instrument="OPTSTK", expiry_dt=EXPIRY,
                                 trade_date="2026-01-06", close=22.0))
        session.commit()
        rules = {
            "legs": [
                {"action": "SELL", "option_type": "CE", "strike_selection": {"mode": "OTM_PERCENT", "value": 10.0}, "lots": 1,
                 "exit": {"trailing": {"enabled": True, "trail_amount": 1.0, "trail_type": "points"}}},
                {"action": "SELL", "option_type": "PE", "strike_selection": {"mode": "OTM_PERCENT", "value": 10.0}, "lots": 1},
            ],
        }
        engine = _build_engine(feed, rules, session=session)
        with patch("utils.instrument_cache.InstrumentCache.resolve_equity_key", return_value=EQUITY_KEY), \
             patch.object(MockBroker, "get_lot_size", return_value=1):
            row = engine._run_one_cycle(dict(_CYCLE))

        assert row is not None
        ce_leg = row["legs"][0]
        assert ce_leg["exit_reason"] == "TRAILING_STOP"
        assert ce_leg["exit_date"] == "2026-01-06"


class TestNaturalExitDateAndTradingDays:
    def test_natural_exit_date_returns_the_last_real_trading_day_for_that_expiry(self, db_session):
        session = db_session
        session.add_all([
            FnoBhavcopy(symbol="TESTSTOCK", instrument="OPTSTK", expiry_dt="2026-01-29", trade_date="2026-01-27"),
            FnoBhavcopy(symbol="TESTSTOCK", instrument="OPTSTK", expiry_dt="2026-01-29", trade_date="2026-01-29"),
            FnoBhavcopy(symbol="TESTSTOCK", instrument="OPTSTK", expiry_dt="2026-02-26", trade_date="2026-02-26"),
        ])
        session.commit()
        engine = CustomRuleBacktestEngine.__new__(CustomRuleBacktestEngine)
        engine.symbol, engine.option_instrument, engine.session = "TESTSTOCK", "OPTSTK", session
        cache = {}
        assert engine._natural_exit_date("2026-01-29", cache) == "2026-01-29"
        assert engine._natural_exit_date("2026-02-26", cache) == "2026-02-26"
        # Memoized — same dict reused without re-querying.
        assert cache["2026-01-29"] == "2026-01-29"

    def test_trading_days_scoped_to_one_expiry_and_date_range(self, db_session):
        session = db_session
        session.add_all([
            FnoBhavcopy(symbol="TESTSTOCK", instrument="OPTSTK", expiry_dt="2026-01-29", trade_date="2026-01-06"),
            FnoBhavcopy(symbol="TESTSTOCK", instrument="OPTSTK", expiry_dt="2026-01-29", trade_date="2026-01-07"),
            FnoBhavcopy(symbol="TESTSTOCK", instrument="OPTSTK", expiry_dt="2026-02-26", trade_date="2026-01-06"),  # different expiry — excluded
        ])
        session.commit()
        engine = CustomRuleBacktestEngine.__new__(CustomRuleBacktestEngine)
        engine.symbol, engine.option_instrument, engine.session = "TESTSTOCK", "OPTSTK", session
        days = engine._trading_days("2026-01-29", "2026-01-05", "2026-01-29")
        assert days == ["2026-01-06", "2026-01-07"]


class TestLegPnlPct:
    def test_sell_leg_profits_as_price_falls(self):
        engine = CustomRuleBacktestEngine.__new__(CustomRuleBacktestEngine)
        leg = {"transaction_type": "SELL", "entry_price": 10.0, "quantity": 50}
        assert engine._leg_pnl_pct(leg, 5.0) == pytest.approx(50.0)
        assert engine._leg_pnl_pct(leg, 15.0) == pytest.approx(-50.0)

    def test_buy_leg_profits_as_price_rises(self):
        engine = CustomRuleBacktestEngine.__new__(CustomRuleBacktestEngine)
        leg = {"transaction_type": "BUY", "entry_price": 10.0, "quantity": 50}
        assert engine._leg_pnl_pct(leg, 15.0) == pytest.approx(50.0)

    def test_missing_price_returns_none(self):
        engine = CustomRuleBacktestEngine.__new__(CustomRuleBacktestEngine)
        leg = {"transaction_type": "SELL", "entry_price": 10.0, "quantity": 50}
        assert engine._leg_pnl_pct(leg, None) is None
