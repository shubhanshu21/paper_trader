"""
tests/test_routes_clone_strategy.py — tests for POST
/{strategy_id}/clone (routes_custom_strategies.py): duplicates a
strategy's config into a fresh DRAFT row, explicitly WITHOUT copying
status/performance/backtest-result/last_entry_date/automation settings.

Direct-function-call style against the shared automate_test MySQL schema
(see tests/conftest.py) — same pattern as
tests/test_routes_backtest_runs.py.
"""
import json

import pytest
from fastapi import HTTPException

from api.routes_custom_strategies import clone_strategy
from db.models import CustomStrategy

USER = {"sub": "1"}
OTHER_USER = {"sub": "2"}

_RULES = {
    "legs": [{"action": "SELL", "option_type": "CE", "strike_selection": {"mode": "OTM_PERCENT", "value": 10.0}, "lots": 2}],
    "expiry": {"mode": "WEEKLY"},
    "exit": {"stop_loss_pct": 25.0, "take_profit_pct": 40.0},
}


def _make_strategy(db, **overrides):
    defaults = {
        "user_id": 1, "name": "Original Strategy", "description": "A test strategy.",
        "instrument_type": "STOCK", "strategy_type": "CUSTOM", "option_type": "CE",
        "symbols": json.dumps(["RELIANCE", "TCS"]), "rules_json": json.dumps(_RULES),
        "num_lots": 3, "take_profit_pct": 40.0, "stop_loss_pct": 25.0, "exit_days_before_expiry": 2,
        "status": "LIVE",
        "backtest_return_pct": 12.5, "paper_return_pct": 8.0, "live_return_pct": 5.5,
        "last_entry_date": json.dumps({"RELIANCE": {"WEEKLY": "2026-08-01"}}),
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


class TestCloneStrategy:
    def test_copies_config_fields(self, db_session, strategy):
        result = clone_strategy(strategy.id, db_session, USER)
        assert result["name"] == "Original Strategy (Copy)"
        assert result["description"] == "A test strategy."
        assert result["instrument_type"] == "STOCK"
        assert result["symbols"] == ["RELIANCE", "TCS"]
        assert result["strategy_type"] == "CUSTOM"
        assert result["option_type"] == "CE"
        assert result["num_lots"] == 3

    def test_rules_json_copied_exactly(self, db_session, strategy):
        result = clone_strategy(strategy.id, db_session, USER)
        clone_row = db_session.query(CustomStrategy).filter(CustomStrategy.id == result["id"]).first()
        assert json.loads(clone_row.rules_json) == _RULES

    def test_clone_always_starts_draft_regardless_of_original_status(self, db_session, strategy):
        assert strategy.status == "LIVE"
        result = clone_strategy(strategy.id, db_session, USER)
        assert result["status"] == "DRAFT"

    def test_performance_figures_not_copied(self, db_session, strategy):
        result = clone_strategy(strategy.id, db_session, USER)
        assert result["backtest_return_pct"] is None
        assert result["paper_return_pct"] is None
        assert result["live_return_pct"] is None

    def test_last_entry_date_not_copied(self, db_session, strategy):
        result = clone_strategy(strategy.id, db_session, USER)
        clone_row = db_session.query(CustomStrategy).filter(CustomStrategy.id == result["id"]).first()
        assert clone_row.last_entry_date is None

    def test_original_untouched(self, db_session, strategy):
        clone_strategy(strategy.id, db_session, USER)
        db_session.refresh(strategy)
        assert strategy.name == "Original Strategy"
        assert strategy.status == "LIVE"
        assert strategy.backtest_return_pct == pytest.approx(12.5)

    def test_creates_a_distinct_row(self, db_session, strategy):
        result = clone_strategy(strategy.id, db_session, USER)
        assert result["id"] != strategy.id
        count = db_session.query(CustomStrategy).filter(CustomStrategy.user_id == 1).count()
        assert count == 2

    def test_not_found_for_other_users_strategy(self, db_session, strategy):
        with pytest.raises(HTTPException) as exc_info:
            clone_strategy(strategy.id, db_session, OTHER_USER)
        assert exc_info.value.status_code == 404
