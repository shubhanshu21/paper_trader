"""
tests/test_partial_fill_auto_unwind.py — Regression test for the naked-
position bug fixed this session: if one leg of the strangle fills and the
other fails, the filled leg must be auto-bought-back (never left naked),
and the strategy must report failure, not a false "success".

Fully hermetic — no network, no real instrument master lookups (patches
InstrumentCache so it doesn't matter what day this runs or whether
cache/upstox_instruments_*.csv exists).
"""
from types import SimpleNamespace
from unittest.mock import patch

from automate.backtest.data_feed import DataFeed
from automate.broker.mock_broker import MockBroker
from automate.compliance.sebi_rules import AuditTrail, KillSwitch, OrderRateLimiter
from automate.strategies.custom.rule_strategy import RuleBasedStrategy
from automate.utils.option_utils import calculate_strangle_strikes

EQUITY_KEY = "NSE_EQ|TEST_ISIN"
SPOT = 1300.0
STRIKE_STEP = 20
FAR_FUTURE_EXPIRY = "2099-12-31"  # always "nearest" regardless of when this test runs


def _build_feed(*, ce_has_ltp: bool, pe_has_ltp: bool) -> DataFeed:
    call_strike, put_strike = calculate_strangle_strikes(SPOT, 0.10, STRIKE_STEP)
    call_token, put_token = "NSE_FO|CE_TEST", "NSE_FO|PE_TEST"

    feed = DataFeed()
    feed.set_ltp(EQUITY_KEY, SPOT)
    feed.set_option_contracts(EQUITY_KEY, [FAR_FUTURE_EXPIRY])
    feed.set_option_chain(EQUITY_KEY, FAR_FUTURE_EXPIRY, [
        SimpleNamespace(strike_price=call_strike, call_options=SimpleNamespace(instrument_key=call_token), put_options=None),
        SimpleNamespace(strike_price=put_strike, call_options=None, put_options=SimpleNamespace(instrument_key=put_token)),
    ])
    if ce_has_ltp:
        feed.set_ltp(call_token, 5.0)
    if pe_has_ltp:
        feed.set_ltp(put_token, 4.0)
    feed.set_time(__import__("datetime").datetime(2026, 1, 5, 10, 0, 0))  # a Monday, in-hours
    return feed


def _build_strategy(feed: DataFeed) -> RuleBasedStrategy:
    broker = MockBroker(data_feed=feed, slippage_pct=0.0)
    audit = AuditTrail(audit_log_path="logs/test_audit_trail.log")
    kill_switch = KillSwitch()
    rate_limiter = OrderRateLimiter(max_per_second=10)
    rules = {
        "legs": [
            {
                "action": "SELL",
                "option_type": "CE",
                "strike_selection": {"mode": "OTM_PERCENT", "value": 10.0},
                "lots": 1,
            },
            {
                "action": "SELL",
                "option_type": "PE",
                "strike_selection": {"mode": "OTM_PERCENT", "value": 10.0},
                "lots": 1,
            },
        ]
    }
    with patch("automate.utils.instrument_cache.InstrumentCache.resolve_equity_key", return_value=EQUITY_KEY), \
         patch.object(MockBroker, "get_lot_size", return_value=1):
        strategy = RuleBasedStrategy(
            broker=broker, audit=audit, kill_switch=kill_switch, rate_limiter=rate_limiter,
            symbol="TESTSTOCK", rules=rules, strike_step=STRIKE_STEP, product="NRML",
        )
    return strategy, broker, kill_switch


class TestBothLegsFillSuccessfully:
    def test_reports_success_and_holds_both_legs(self):
        feed = _build_feed(ce_has_ltp=True, pe_has_ltp=True)
        strategy, broker, kill_switch = _build_strategy(feed)

        result = strategy.run()

        assert result["status"] in ("success", "dry_run")
        assert kill_switch.is_active() is False
        # Both legs open, nothing bought back.
        assert sum(1 for o in broker.orders if o["transaction_type"] == "BUY") == 0
        assert sum(1 for o in broker.orders if o["transaction_type"] == "SELL") == 2


class TestPartialFillTriggersAutoUnwind:
    """The exact bug scenario from this session: CE fills, PE has no LTP and fails."""

    def test_filled_leg_is_bought_back_not_left_naked(self):
        feed = _build_feed(ce_has_ltp=True, pe_has_ltp=False)
        strategy, broker, kill_switch = _build_strategy(feed)

        result = strategy.run()

        # Must NOT report success — a naked position must never look fine.
        assert result["status"] == "failed"
        # Kill switch must halt further order flow after a partial fill.
        assert kill_switch.is_active() is True

        sells = [o for o in broker.orders if o["transaction_type"] == "SELL"]
        buys = [o for o in broker.orders if o["transaction_type"] == "BUY"]
        assert len(sells) == 1  # only the CE leg actually sold
        assert len(buys) == 1  # and it was immediately bought back
        assert sells[0]["instrument_token"] == buys[0]["instrument_token"]

        # Net position must be flat — zero, not short.
        for token, qty in broker.positions.items():
            assert qty == 0, f"{token} left with a naked position: {qty}"

    def test_reverse_case_pe_fills_ce_fails(self):
        feed = _build_feed(ce_has_ltp=False, pe_has_ltp=True)
        strategy, broker, _kill_switch = _build_strategy(feed)

        result = strategy.run()

        assert result["status"] == "failed"
        for token, qty in broker.positions.items():
            assert qty == 0, f"{token} left with a naked position: {qty}"


class TestBothLegsFail:
    def test_no_orders_placed_when_neither_leg_has_ltp(self):
        feed = _build_feed(ce_has_ltp=False, pe_has_ltp=False)
        strategy, broker, kill_switch = _build_strategy(feed)

        result = strategy.run()

        assert result["status"] == "failed"
        assert len(broker.orders) == 0
        assert kill_switch.is_active() is True


class TestUnwindRetryAndEscalation:
    """Covers the retry/escalation logic added to _unwind_filled_legs()."""

    def test_unwind_succeeds_after_transient_failure(self):
        feed = _build_feed(ce_has_ltp=True, pe_has_ltp=False)
        strategy, broker, _kill_switch = _build_strategy(feed)

        real_place_buy_order = broker.place_buy_order
        calls = {"n": 0}

        def flaky_place_buy_order(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("simulated transient network error")
            return real_place_buy_order(*args, **kwargs)

        with patch.object(broker, "place_buy_order", side_effect=flaky_place_buy_order), \
             patch("automate.strategies.custom.rule_strategy.RuleBasedStrategy._UNWIND_RETRY_DELAY_SEC", 0.0):
            strategy.run()

        assert calls["n"] == 2  # failed once, succeeded on retry
        buys = [o for o in broker.orders if o["transaction_type"] == "BUY"]
        assert len(buys) == 1  # the unwind did eventually complete
        for _token, qty in broker.positions.items():
            assert qty == 0

    def test_writes_alert_file_when_all_retries_exhausted(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)  # so logs/ writes land in a throwaway dir
        feed = _build_feed(ce_has_ltp=True, pe_has_ltp=False)
        strategy, broker, _kill_switch = _build_strategy(feed)

        with patch.object(broker, "place_buy_order", side_effect=RuntimeError("simulated permanent failure")), \
             patch("automate.strategies.custom.rule_strategy.RuleBasedStrategy._UNWIND_RETRY_DELAY_SEC", 0.0):
            result = strategy.run()

        assert result["status"] == "failed"
        alert_files = list((tmp_path / "logs").glob("ALERT_MANUAL_INTERVENTION_*.flag"))
        assert len(alert_files) == 1
        content = alert_files[0].read_text()
        assert "TESTSTOCK" in content
        assert "MANUAL INTERVENTION REQUIRED" in content
        assert "simulated permanent failure" in content
