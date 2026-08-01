"""
tests/test_custom_backtest_engine.py — Regression test for this session's
exit-slippage fix in backtest/custom_engine.py::CustomRuleBacktestEngine
._run_one_cycle(): exit fills must go through MockBroker (picking up its
slippage_pct) exactly like entries already do, not read the raw feed price
directly — the bug made every backtest systematically more optimistic
leaving a position than entering one.

CustomRuleBacktestEngine.__init__ hardcodes a real MySQL SessionLocal() +
BhavcopyDataFeed, which this test avoids entirely: the engine is built via
__new__() (bypassing __init__) with its attributes wired to the same
DataFeed/MockBroker test-double pattern tests/test_partial_fill_auto_unwind.py
already established, and rules configured with no TP/SL/exit-days-before-
expiry trigger — so _run_one_cycle() never touches self.session (which is
left None) and exit_reason is always "EXPIRY", exit_date == cycle["exit_date"]
as passed in. Fully hermetic — no network, no DB.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from automate.backtest.custom_engine import CustomRuleBacktestEngine
from automate.backtest.data_feed import DataFeed
from automate.broker.mock_broker import MockBroker
from automate.compliance.sebi_rules import AuditTrail, KillSwitch, OrderRateLimiter
from automate.utils.option_utils import calculate_strangle_strikes

EQUITY_KEY = "NSE_EQ|TEST_ISIN"
SPOT = 1300.0
STRIKE_STEP = 20
FAR_FUTURE_EXPIRY = "2099-12-31"
SLIPPAGE_PCT = 0.02  # exaggerated vs. the real 0.001 default, so it's unmistakable in assertions


class _TestFeed(DataFeed):
    """DataFeed + a get_volume() stub — _run_one_cycle()'s liquidity check needs it, real DataFeed doesn't implement it."""
    def get_volume(self, instrument_key: str) -> int:
        return 1


def _build_feed(ce_price: float, pe_price: float) -> _TestFeed:
    call_strike, put_strike = calculate_strangle_strikes(SPOT, 0.10, STRIKE_STEP)
    call_token, put_token = "NSE_FO|CE_TEST", "NSE_FO|PE_TEST"

    feed = _TestFeed()
    feed.set_ltp(EQUITY_KEY, SPOT)
    feed.set_option_contracts(EQUITY_KEY, [FAR_FUTURE_EXPIRY])
    feed.set_option_chain(EQUITY_KEY, FAR_FUTURE_EXPIRY, [
        SimpleNamespace(strike_price=call_strike, call_options=SimpleNamespace(instrument_key=call_token), put_options=None),
        SimpleNamespace(strike_price=put_strike, call_options=None, put_options=SimpleNamespace(instrument_key=put_token)),
    ])
    feed.set_ltp(call_token, ce_price)
    feed.set_ltp(put_token, pe_price)
    return feed


def _build_engine(feed: _TestFeed, rules: dict) -> CustomRuleBacktestEngine:
    engine = CustomRuleBacktestEngine.__new__(CustomRuleBacktestEngine)
    engine.symbol = "TESTSTOCK"
    engine.rules = rules
    engine.strike_step = STRIKE_STEP
    engine.product = "NRML"
    engine.option_instrument = "OPTSTK"
    engine.future_instrument = "FUTSTK"
    engine.session = None  # never touched: rules below have no TP/SL/exit-days trigger
    engine.equity_key = EQUITY_KEY
    engine.feed = feed
    engine.broker = MockBroker(data_feed=feed, slippage_pct=SLIPPAGE_PCT)
    engine.audit = AuditTrail(audit_log_path="logs/test_audit_trail.log")
    engine.rate_limiter = OrderRateLimiter(max_per_second=10)
    return engine


_RULES = {
    "legs": [
        {"action": "SELL", "option_type": "CE", "strike_selection": {"mode": "OTM_PERCENT", "value": 10.0}, "lots": 1},
        {"action": "SELL", "option_type": "PE", "strike_selection": {"mode": "OTM_PERCENT", "value": 10.0}, "lots": 1},
    ],
    # No take_profit_pct/stop_loss_pct/exit_days_before_expiry — the
    # day-by-day trigger walk (the only place _run_one_cycle would need a
    # real self.session) never runs; exit_reason stays "EXPIRY".
}

_CYCLE = {"entry_date": "2026-01-05", "expiry": "2026-01-29", "exit_date": "2026-01-29"}


def _run_cycle(ce_price: float, pe_price: float):
    feed = _build_feed(ce_price, pe_price)
    engine = _build_engine(feed, _RULES)
    with patch("automate.utils.instrument_cache.InstrumentCache.resolve_equity_key", return_value=EQUITY_KEY), \
         patch.object(MockBroker, "get_lot_size", return_value=1):
        return engine._run_one_cycle(dict(_CYCLE))


class TestExitSlippageFix:
    def test_exit_price_includes_slippage_not_raw_ltp(self):
        row = _run_cycle(ce_price=5.0, pe_price=4.0)
        assert row is not None

        for leg in row["legs"]:
            raw_ltp = 5.0 if leg["option_type"] == "CE" else 4.0
            # Entry was SELL -> exit is BUY -> MockBroker's slippage makes
            # a BUY fill worse (higher) than the raw print.
            expected_exit = round(raw_ltp * (1.0 + SLIPPAGE_PCT), 4)
            assert leg["exit_price"] == pytest.approx(expected_exit, abs=1e-6)
            assert leg["exit_price"] != raw_ltp  # the exact bug this fix closes
            assert leg["exit_order_id"] is not None
            assert leg["exit_order_id"].startswith("MOCK-")

    def test_entry_and_exit_slippage_are_symmetric_for_a_sell_leg(self):
        row = _run_cycle(ce_price=5.0, pe_price=4.0)
        for leg in row["legs"]:
            raw_ltp = 5.0 if leg["option_type"] == "CE" else 4.0
            entry_slip = raw_ltp - leg["entry_price"]   # SELL entry: worse = lower
            exit_slip = leg["exit_price"] - raw_ltp      # BUY exit (closing a SELL): worse = higher
            assert entry_slip == pytest.approx(exit_slip, abs=1e-6)

    def test_flat_price_cycle_still_shows_a_net_loss_from_round_trip_slippage_and_costs(self):
        # Same price at entry and exit ("no move") — a real trader still
        # pays slippage + costs both ways, so net P&L must be negative,
        # not zero. This was silently NOT true before the exit-slippage fix.
        row = _run_cycle(ce_price=5.0, pe_price=4.0)
        assert row["net_pnl"] < 0
        assert row["won"] is False
