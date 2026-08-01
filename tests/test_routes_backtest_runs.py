"""
tests/test_routes_backtest_runs.py — Tests for the async backtest
run/history endpoints added to routes_custom_strategies.py this session:
POST /{id}/backtest (queues a CustomBacktestRun row, returns immediately),
GET /{id}/backtest/runs (history list), GET /{id}/backtest/runs/{run_id}
(poll), and the background _run_backtest_sync()/_run_backtest_symbols()
functions that actually do the work.

Direct-function-call style (this session's established pattern — see
tests/test_routes_advanced_orders.py) against an in-memory SQLite DB.
CustomRuleBacktestEngine and compute_nifty_benchmark_return are patched at
their source module so no real bhavcopy data / MySQL is needed.
"""
import asyncio
import json
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.types import BigInteger


@compiles(BigInteger, "sqlite")
def compile_bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"


from automate.db.engine import Base
from automate.db.models import CustomBacktestRun, CustomStrategy
import automate.api.routes_custom_strategies as routes
from automate.api.routes_custom_strategies import (
    BacktestRequest, _run_backtest_sync, _run_backtest_symbols,
    backtest_strategy, get_backtest_run, list_backtest_runs,
)

USER = {"sub": "1"}
OTHER_USER = {"sub": "2"}

_RULES = {
    "legs": [{"action": "SELL", "option_type": "CE", "strike_selection": {"mode": "OTM_PERCENT", "value": 10.0}, "lots": 1}],
    "expiry": {"mode": "MONTHLY"},
}


def _fake_cycle(entry, exit_, pnl_pct, won=True):
    return {
        "entry_date": entry, "exit_date": exit_, "expiry": exit_,
        "pnl_pct_of_premium": pnl_pct, "net_pnl": pnl_pct * 50.0, "won": won,
        "exit_reason": "EXPIRY", "legs": [],
    }


class FakeEngine:
    """Stands in for CustomRuleBacktestEngine — returns canned cycles per symbol, no DB/bhavcopy needed."""
    _CYCLES_BY_SYMBOL = {
        "RELIANCE": [_fake_cycle("2026-01-05", "2026-01-29", 8.0), _fake_cycle("2026-02-02", "2026-02-26", -3.0, won=False)],
        "TCS": [_fake_cycle("2026-01-06", "2026-01-29", 5.0)],
        "BROKEN": None,  # triggers RuntimeError below
    }

    def __init__(self, symbol, rules, option_instrument=None, future_instrument=None):
        self.symbol = symbol
        if self._CYCLES_BY_SYMBOL.get(symbol) is None and symbol == "BROKEN":
            raise RuntimeError("could not resolve instrument key for BROKEN")

    def run(self, from_date=None, to_date=None, on_progress=None):
        cycles = self._CYCLES_BY_SYMBOL.get(self.symbol, [])
        total = len(cycles)
        for i in range(total):
            if on_progress:
                on_progress(i + 1, total)
        return cycles


@pytest.fixture()
def session_factory():
    # SQLite :memory: uses one pooled connection per thread by default, so
    # multiple sessions from this SAME sessionmaker (bound to the same
    # engine) all see the same data as long as everything runs on this
    # test's thread — which _run_backtest_sync() does here, since it's
    # called directly rather than via asyncio.to_thread.
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[CustomBacktestRun.__table__, CustomStrategy.__table__])
    return sessionmaker(bind=engine)


@pytest.fixture()
def db(session_factory):
    session = session_factory()
    yield session
    session.close()


def _make_strategy(db, **overrides):
    defaults = dict(
        user_id=1, name="Test Strategy", instrument_type="STOCK", strategy_type="CUSTOM", option_type="BOTH",
        symbols=json.dumps(["RELIANCE", "TCS"]), rules_json=json.dumps(_RULES), status="DRAFT",
    )
    defaults.update(overrides)
    s = CustomStrategy(**defaults)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


@pytest.fixture()
def strategy(db):
    return _make_strategy(db)


@pytest.fixture()
def sqlite_session_local(session_factory, db, monkeypatch):
    """
    Patch SessionLocal so _run_backtest_sync()'s own `db = SessionLocal()`
    call (and its `db.close()` at the end) gets a FRESH session from the
    same sessionmaker/engine as the test's `db` fixture — not literally
    the test's own session object, which _run_backtest_sync() would close
    out from under the test.
    """
    monkeypatch.setattr(routes, "SessionLocal", session_factory)
    return db


class TestRunBacktestSymbols:
    def test_merges_and_tags_cycles_chronologically(self):
        with patch("automate.backtest.custom_engine.CustomRuleBacktestEngine", FakeEngine):
            cycles, per_symbol, skipped = _run_backtest_symbols(["TCS", "RELIANCE"], _RULES, "STOCK", None, None)

        assert [c["entry_date"] for c in cycles] == ["2026-01-05", "2026-01-06", "2026-02-02"]
        assert {c["symbol"] for c in cycles} == {"TCS", "RELIANCE"}
        assert per_symbol["RELIANCE"]["cycles_tested"] == 2
        assert per_symbol["TCS"]["cycles_tested"] == 1
        assert skipped == {}

    def test_skips_symbol_that_raises_but_keeps_others(self):
        with patch("automate.backtest.custom_engine.CustomRuleBacktestEngine", FakeEngine):
            cycles, per_symbol, skipped = _run_backtest_symbols(["RELIANCE", "BROKEN"], _RULES, "STOCK", None, None)

        assert len(cycles) == 2
        assert "BROKEN" in skipped
        assert "RELIANCE" in per_symbol

    def test_progress_callback_receives_symbol_and_counts(self):
        seen = []
        with patch("automate.backtest.custom_engine.CustomRuleBacktestEngine", FakeEngine):
            _run_backtest_symbols(["TCS"], _RULES, "STOCK", None, None, on_progress=lambda sym, d, t: seen.append((sym, d, t)))
        assert seen == [("TCS", 1, 1)]


class TestRunBacktestSync:
    def test_completes_and_populates_result(self, db, strategy, sqlite_session_local):
        run = CustomBacktestRun(strategy_id=strategy.id, user_id=1, status="QUEUED", rules_snapshot_json=json.dumps(_RULES))
        db.add(run)
        db.commit()
        db.refresh(run)

        with patch("automate.backtest.custom_engine.CustomRuleBacktestEngine", FakeEngine), \
             patch("automate.backtest.custom_engine.compute_nifty_benchmark_return", return_value=6.0):
            _run_backtest_sync(run.id)

        db.refresh(run)
        assert run.status == "COMPLETED"
        assert run.completed_at is not None
        result = json.loads(run.result_json)
        assert result["cycles_tested"] == 3
        assert result["per_symbol"].keys() == {"RELIANCE", "TCS"}
        assert result["benchmark_return_pct"] == 6.0
        assert result["sharpe_ratio"] is not None or result["sharpe_ratio"] is None  # just must not KeyError
        assert "cagr_pct" in result

        db.refresh(strategy)
        assert strategy.status == "BACKTESTING"
        assert strategy.backtest_result_json is not None

    def test_all_symbols_failing_marks_run_failed(self, db, strategy, sqlite_session_local):
        strategy.symbols = json.dumps(["BROKEN"])
        db.commit()
        run = CustomBacktestRun(strategy_id=strategy.id, user_id=1, status="QUEUED", rules_snapshot_json=json.dumps(_RULES))
        db.add(run)
        db.commit()
        db.refresh(run)

        with patch("automate.backtest.custom_engine.CustomRuleBacktestEngine", FakeEngine):
            _run_backtest_sync(run.id)

        db.refresh(run)
        assert run.status == "FAILED"
        assert run.error_message is not None

    def test_progress_updates_during_run(self, db, strategy, sqlite_session_local):
        run = CustomBacktestRun(strategy_id=strategy.id, user_id=1, status="QUEUED", rules_snapshot_json=json.dumps(_RULES))
        db.add(run)
        db.commit()
        db.refresh(run)

        with patch("automate.backtest.custom_engine.CustomRuleBacktestEngine", FakeEngine), \
             patch("automate.backtest.custom_engine.compute_nifty_benchmark_return", return_value=None):
            _run_backtest_sync(run.id)

        db.refresh(run)
        assert run.progress_current == run.progress_total


class TestBacktestStrategyEndpoint:
    def test_queues_run_and_returns_run_id(self, db, strategy, monkeypatch):
        # backtest_strategy is `async def` (see routes_custom_strategies.py —
        # must run ON the event loop for asyncio.create_task() inside it to
        # find a running loop; a plain `def` route runs in FastAPI's
        # threadpool instead, where create_task() raises "no running event
        # loop" — the exact bug this test guards against regressing).
        monkeypatch.setattr(routes.asyncio, "create_task", lambda coro: coro.close())
        resp = asyncio.run(backtest_strategy(strategy.id, BacktestRequest(), db, USER))

        assert resp["status"] == "QUEUED"
        row = db.query(CustomBacktestRun).filter(CustomBacktestRun.id == resp["run_id"]).first()
        assert row is not None
        assert row.status == "QUEUED"
        assert json.loads(row.rules_snapshot_json) == _RULES

    def test_rejects_strategy_with_no_rules(self, db):
        s = _make_strategy(db, name="No rules", symbols=json.dumps(["RELIANCE"]), rules_json=None)
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(backtest_strategy(s.id, BacktestRequest(), db, USER))
        assert exc_info.value.status_code == 400

    def test_rejects_commodity_strategy(self, db):
        s = _make_strategy(db, name="Commodity", instrument_type="COMMODITY", symbols=json.dumps(["GOLD"]))
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(backtest_strategy(s.id, BacktestRequest(), db, USER))
        assert exc_info.value.status_code == 400


class TestRunHistoryEndpoints:
    def test_list_returns_newest_first_without_full_result(self, db, strategy):
        for i in range(3):
            db.add(CustomBacktestRun(strategy_id=strategy.id, user_id=1, status="COMPLETED", rules_snapshot_json="{}", result_json=json.dumps({"big": "blob"})))
        db.commit()

        resp = list_backtest_runs(strategy.id, 20, db, USER)
        assert len(resp["runs"]) == 3
        assert "result" not in resp["runs"][0]

    def test_get_run_includes_result(self, db, strategy):
        run = CustomBacktestRun(strategy_id=strategy.id, user_id=1, status="COMPLETED", rules_snapshot_json="{}", result_json=json.dumps({"cycles_tested": 5}))
        db.add(run)
        db.commit()
        db.refresh(run)

        resp = get_backtest_run(strategy.id, run.id, db, USER)
        assert resp["result"]["cycles_tested"] == 5

    def test_other_user_cannot_see_run(self, db, strategy):
        run = CustomBacktestRun(strategy_id=strategy.id, user_id=1, status="COMPLETED", rules_snapshot_json="{}")
        db.add(run)
        db.commit()
        db.refresh(run)

        with pytest.raises(HTTPException) as exc_info:
            get_backtest_run(strategy.id, run.id, db, OTHER_USER)
        assert exc_info.value.status_code == 404
