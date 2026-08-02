"""
tests/test_routes_create_strategy.py — POST /custom-strategies now accepts
COMMODITY (previously blocked outright — 'This strategy builder only
supports INDEX and STOCK options', even though the DB model/backtest
route already treated COMMODITY as a real, if backtest-restricted, value)
and backtest_strategy() now also blocks an all-EQUITY strategy (its
cycle-discovery model is expiry-driven — see custom_engine.py — a plain
equity leg has no expiry to anchor a cycle to).

Direct-function-call style against the shared automate_test MySQL schema
(see tests/conftest.py) — same pattern as tests/test_routes_backtest_runs.py.
"""
import asyncio
import json

import pytest
from fastapi import HTTPException

from automate.db.models import CustomStrategy, CustomBacktestRun
from automate.api.routes_custom_strategies import CustomStrategyCreate, create_strategy, backtest_strategy, BacktestRequest

USER = {"sub": "1"}


@pytest.fixture()
def session_factory(db_session_factory):
    return db_session_factory


@pytest.fixture()
def db(db_session):
    return db_session


def _make_strategy(db, **overrides):
    defaults = dict(
        user_id=1, name="Test Strategy", instrument_type="STOCK", strategy_type="CUSTOM", option_type="BOTH",
        symbols=json.dumps(["RELIANCE"]),
        rules_json=json.dumps({
            "legs": [{"action": "SELL", "option_type": "CE", "strike_selection": {"mode": "ATM", "value": None}, "lots": 1}],
        }),
        status="DRAFT",
    )
    defaults.update(overrides)
    s = CustomStrategy(**defaults)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


class TestCreateStrategyAcceptsCommodity:
    def test_commodity_instrument_type_is_accepted(self, db):
        payload = CustomStrategyCreate(
            name="Gold Strangle", instrument_type="COMMODITY", symbols=["GOLD"],
            rules={
                "legs": [{"action": "SELL", "option_type": "CE", "strike_selection": {"mode": "ATM", "value": None}, "lots": 1}],
                "entry": {"mode": "IMMEDIATE", "time": None},
                "exit": {"take_profit_pct": None, "stop_loss_pct": None, "exit_time": None, "exit_days_before_expiry": 0},
            },
        )
        result = create_strategy(payload, db, USER)
        assert result["instrument_type"] == "COMMODITY"
        assert result["status"] == "DRAFT"

    def test_still_rejects_a_genuinely_unknown_instrument_type(self, db):
        payload = CustomStrategyCreate(name="Bad", instrument_type="CRYPTO", symbols=["BTC"], rules={"legs": []})
        with pytest.raises(HTTPException) as exc_info:
            create_strategy(payload, db, USER)
        assert exc_info.value.status_code == 422


class TestBacktestBlocksAllEquityStrategy:
    def test_all_equity_strategy_is_refused(self, db):
        s = _make_strategy(db, name="Equity only", rules_json=json.dumps({
            "legs": [{"instrument_type": "EQUITY", "action": "BUY", "lots": 10}],
        }))
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(backtest_strategy(s.id, BacktestRequest(), db, USER))
        assert exc_info.value.status_code == 400
        assert "EQUITY" in exc_info.value.detail

    def test_mixed_equity_and_option_strategy_is_not_blocked_by_the_equity_check(self, db, monkeypatch):
        import automate.api.routes_custom_strategies as routes
        # Never let this test's background task actually run against a
        # real broker/DB (see this session's standing rule against
        # triggering scheduler/engine logic against production data) —
        # this test only cares whether the QUEUEING request itself passes
        # the all-EQUITY guard, not what the queued run eventually does.
        monkeypatch.setattr(routes.asyncio, "create_task", lambda coro: coro.close())
        s = _make_strategy(db, name="Mixed", rules_json=json.dumps({
            "legs": [
                {"instrument_type": "EQUITY", "action": "BUY", "lots": 10},
                {"action": "SELL", "option_type": "CE", "strike_selection": {"mode": "ATM", "value": None}, "lots": 1},
            ],
        }))
        result = asyncio.run(backtest_strategy(s.id, BacktestRequest(), db, USER))
        assert result["status"] == "QUEUED"
