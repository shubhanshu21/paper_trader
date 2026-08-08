"""
tests/test_routes_optimize.py — tests for POST /{strategy_id}/optimize
(routes_custom_strategies.py), the grid-search endpoint over
utils/optimizer.py. Same direct-function-call / FakeEngine pattern as
tests/test_routes_backtest_runs.py — CustomRuleBacktestEngine is patched
at its source module so no real bhavcopy data is needed, and each grid
point's cycles vary by the stop_loss_pct actually threaded into `rules`
(confirming the endpoint really does rebuild+rerun per combination, not
just the same backtest repeated).
"""
import asyncio
import json

import pytest
from fastapi import HTTPException

from api.routes_custom_strategies import OptimizeRequest, optimize_strategy
from db.models import CustomStrategy

USER = {"sub": "1"}

_RULES = {
    "legs": [{"action": "SELL", "option_type": "CE", "strike_selection": {"mode": "OTM_PERCENT", "value": 10.0}, "lots": 1}],
    "expiry": {"mode": "MONTHLY"},
    "exit": {"stop_loss_pct": 50.0, "take_profit_pct": 50.0},
}


def _fake_cycle(entry, exit_, pnl_pct, won=True):
    return {
        "entry_date": entry, "exit_date": exit_, "expiry": exit_,
        "pnl_pct_of_premium": pnl_pct, "net_pnl": pnl_pct * 50.0, "won": won,
        "exit_reason": "EXPIRY", "legs": [],
    }


class StopLossAwareFakeEngine:
    """
    Cycles depend on the rules actually passed in — a tight stop_loss_pct
    (<=15) produces small, mostly-losing cycles; a loose one produces
    larger, mostly-winning cycles. Lets tests confirm the endpoint threads
    each grid point's parameters into a genuinely different backtest, not
    the same one N times.
    """
    def __init__(self, symbol, rules, option_instrument=None, future_instrument=None, charge_rates=None):
        self.symbol = symbol
        self.stop_loss_pct = rules["exit"]["stop_loss_pct"]

    def run(self, from_date=None, to_date=None, on_progress=None):
        if self.stop_loss_pct <= 15:
            return [_fake_cycle("2026-01-05", "2026-01-29", -2.0, won=False), _fake_cycle("2026-02-02", "2026-02-26", -1.0, won=False)]
        return [_fake_cycle("2026-01-05", "2026-01-29", 8.0), _fake_cycle("2026-02-02", "2026-02-26", 9.0)]


def _make_strategy(db, **overrides):
    defaults = {
        "user_id": 1, "name": "Test Strategy", "instrument_type": "STOCK", "strategy_type": "CUSTOM", "option_type": "BOTH",
        "symbols": json.dumps(["RELIANCE"]), "rules_json": json.dumps(_RULES), "status": "DRAFT",
    }
    defaults.update(overrides)
    s = CustomStrategy(**defaults)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


@pytest.fixture()
def strategy(db_session):
    return _make_strategy(db_session)


class TestOptimizeEndpoint:
    def test_ranks_grid_points_by_sharpe(self, db_session, strategy, monkeypatch):
        monkeypatch.setattr("backtest.custom_engine.CustomRuleBacktestEngine", StopLossAwareFakeEngine)
        req = OptimizeRequest(param_grid={"stop_loss_pct": [10.0, 50.0]})
        resp = asyncio.run(optimize_strategy(strategy.id, req, db_session, USER))

        assert resp["combinations_tested"] == 2
        assert len(resp["results"]) == 2
        # The 50% stop-loss combo (all-winning fake cycles) should rank above the 10% one (all-losing).
        assert resp["results"][0]["params"] == {"stop_loss_pct": 50.0}
        assert resp["results"][0]["sharpe_ratio"] is not None

    def test_two_param_grid(self, db_session, strategy, monkeypatch):
        monkeypatch.setattr("backtest.custom_engine.CustomRuleBacktestEngine", StopLossAwareFakeEngine)
        req = OptimizeRequest(param_grid={"stop_loss_pct": [10.0, 50.0], "take_profit_pct": [20.0, 30.0]})
        resp = asyncio.run(optimize_strategy(strategy.id, req, db_session, USER))
        assert resp["combinations_tested"] == 4
        assert len(resp["results"]) == 4

    def test_rejects_grid_exceeding_cap(self, db_session, strategy):
        req = OptimizeRequest(param_grid={"stop_loss_pct": list(range(30))})
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(optimize_strategy(strategy.id, req, db_session, USER))
        assert exc_info.value.status_code == 400

    def test_rejects_unsupported_param(self, db_session, strategy):
        req = OptimizeRequest(param_grid={"strike_offset": [1.0, 2.0]})
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(optimize_strategy(strategy.id, req, db_session, USER))
        assert exc_info.value.status_code == 400

    def test_rejects_strategy_with_no_rules(self, db_session):
        s = _make_strategy(db_session, name="No rules", rules_json=None)
        req = OptimizeRequest(param_grid={"stop_loss_pct": [10.0]})
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(optimize_strategy(s.id, req, db_session, USER))
        assert exc_info.value.status_code == 400

    def test_rejects_commodity_strategy(self, db_session):
        s = _make_strategy(db_session, name="Commodity", instrument_type="COMMODITY", symbols=json.dumps(["GOLD"]))
        req = OptimizeRequest(param_grid={"stop_loss_pct": [10.0]})
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(optimize_strategy(s.id, req, db_session, USER))
        assert exc_info.value.status_code == 400
