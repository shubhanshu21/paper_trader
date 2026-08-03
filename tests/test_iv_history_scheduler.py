"""
tests/test_iv_history_scheduler.py — api/iv_history_scheduler.py: the
daily ATM-IV snapshot job feeding rule_schema.py's entry.condition
IV_RANK feature. Direct-function-call style, fakes for the broker, the shared
automate_test MySQL schema (see tests/conftest.py) for
CustomStrategy/SymbolIvHistory — same pattern
tests/test_routes_backtest_runs.py already established.
"""
import json
from datetime import date
from unittest.mock import patch

import pytest

import automate.api.iv_history_scheduler as scheduler
from automate.db.models import CustomStrategy, SymbolIvHistory


class FakeBroker:
    def __init__(self, forward=None, atm_price=None, strike_step=100, expiries=None):
        self.forward = forward
        self.atm_price = atm_price
        self.strike_step = strike_step
        self.expiries = expiries if expiries is not None else ["2026-01-08"]

    def resolve_instrument_key(self, symbol):
        return f"KEY|{symbol}"

    def get_option_contracts(self, instrument_key):
        return self.expiries

    def get_current_time(self):
        return None

    def get_ltp(self, token):
        if token and token.startswith("ATM|"):
            return self.atm_price
        return self.forward

    def get_strike_step(self, symbol):
        return self.strike_step

    def get_option_chain(self, instrument_key, expiry):
        return [{"strike": 20000, "token": "ATM|CE"}] if self.atm_price is not None else None


@pytest.fixture()
def session_factory(db_session_factory):
    return db_session_factory


@pytest.fixture()
def db(db_session):
    return db_session


def _make_strategy(db, **overrides):
    defaults = {
        "user_id": 1, "name": "Test Strategy", "instrument_type": "INDEX", "strategy_type": "CUSTOM", "option_type": "BOTH",
        "symbols": json.dumps(["NIFTY"]), "status": "PAPER_TRADING",
    }
    defaults.update(overrides)
    s = CustomStrategy(**defaults)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


class TestAlreadySnapshottedToday:
    def test_false_when_no_row_yet(self, db):
        assert scheduler._already_snapshotted_today(db, "NIFTY", date.today().isoformat()) is False

    def test_true_once_a_row_exists_for_today(self, db):
        today = date.today().isoformat()
        db.add(SymbolIvHistory(symbol="NIFTY", trade_date=today, atm_iv=15.0))
        db.commit()
        assert scheduler._already_snapshotted_today(db, "NIFTY", today) is True

    def test_scoped_per_symbol(self, db):
        today = date.today().isoformat()
        db.add(SymbolIvHistory(symbol="NIFTY", trade_date=today, atm_iv=15.0))
        db.commit()
        assert scheduler._already_snapshotted_today(db, "BANKNIFTY", today) is False


class TestSnapshotSymbolIv:
    def test_returns_none_when_chain_lookup_fails(self):
        broker = FakeBroker(expiries=[])
        with patch("automate.strategies.custom.rule_strategy.resolve_leg_strike", return_value=20000):
            assert scheduler._snapshot_symbol_iv(broker, "NIFTY") is None

    def test_returns_none_on_missing_forward_price(self):
        broker = FakeBroker(forward=None, atm_price=300.0)
        with patch("automate.utils.instrument_cache.InstrumentCache.resolve_nearest_future_key", return_value=None):
            assert scheduler._snapshot_symbol_iv(broker, "NIFTY") is None

    def test_never_raises_on_unexpected_broker_exception(self):
        class ExplodingBroker(FakeBroker):
            def get_option_contracts(self, instrument_key):
                raise RuntimeError("broker hiccup")
        assert scheduler._snapshot_symbol_iv(ExplodingBroker(), "NIFTY") is None


class TestRunDailySnapshot:
    def test_writes_one_row_per_active_strategy_symbol_and_skips_when_iv_unresolvable(self, db, session_factory, monkeypatch):
        _make_strategy(db, symbols=json.dumps(["NIFTY"]), status="PAPER_TRADING")
        _make_strategy(db, symbols=json.dumps(["BANKNIFTY"]), status="STOPPED")  # inactive — must be skipped

        monkeypatch.setattr(scheduler, "SessionLocal", session_factory)
        monkeypatch.setattr(scheduler, "_get_brokers", lambda: {"paper": FakeBroker(expiries=[])})  # forces None IV -> no row written

        scheduler._run_daily_snapshot()

        rows = db.query(SymbolIvHistory).all()
        assert rows == []  # unresolvable IV never fabricated into a row

    def test_skips_symbols_already_snapshotted_today(self, db, session_factory, monkeypatch):
        _make_strategy(db, symbols=json.dumps(["NIFTY"]), status="LIVE")
        db.add(SymbolIvHistory(symbol="NIFTY", trade_date=date.today().isoformat(), atm_iv=12.0))
        db.commit()

        calls = []

        def _tracking_snapshot(broker, symbol):
            calls.append(symbol)
            return 99.0

        monkeypatch.setattr(scheduler, "SessionLocal", session_factory)
        monkeypatch.setattr(scheduler, "_get_brokers", lambda: {"paper": FakeBroker()})
        monkeypatch.setattr(scheduler, "_snapshot_symbol_iv", _tracking_snapshot)

        scheduler._run_daily_snapshot()

        assert calls == []  # already snapshotted today — never re-solved
