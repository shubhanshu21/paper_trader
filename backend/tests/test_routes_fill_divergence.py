"""
tests/test_routes_fill_divergence.py — tests for GET
/{strategy_id}/fill-divergence (routes_custom_strategies.py), which reads
real CustomStrategyPosition rows split by mode and delegates the actual
math to utils/fill_divergence.py (already unit-tested in
tests/test_fill_divergence.py — this file only checks the endpoint wires
DB rows into that function correctly, ownership, and ignores other
strategies' legs).

Direct-function-call style against the shared automate_test MySQL schema
(see tests/conftest.py) — same pattern as tests/test_routes_backtest_runs.py.
"""
import json
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

from api.routes_custom_strategies import get_fill_divergence
from db.models import CustomStrategy, CustomStrategyPosition

USER = {"sub": "1"}
OTHER_USER = {"sub": "2"}


def _make_strategy(db, **overrides):
    defaults = {
        "user_id": 1, "name": "Test Strategy", "instrument_type": "STOCK", "strategy_type": "CUSTOM", "option_type": "BOTH",
        "symbols": json.dumps(["RELIANCE"]),
        "rules_json": json.dumps({"legs": [{"action": "SELL", "option_type": "CE", "strike_selection": {"mode": "ATM", "value": None}, "lots": 1}]}),
        "status": "LIVE",
    }
    defaults.update(overrides)
    s = CustomStrategy(**defaults)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _make_leg(db, strategy_id, mode, entry_price, exit_price, status="CLOSED", exit_reason="TARGET", hours_held=6):
    opened = datetime(2026, 1, 1, 9, 20)
    closed = opened + timedelta(hours=hours_held) if status == "CLOSED" else None
    leg = CustomStrategyPosition(
        strategy_id=strategy_id, leg_index=0, mode=mode, instrument_key="NSE_FO|TEST",
        instrument_type="OPTION", option_type="CE", transaction_type="SELL",
        quantity=50, entry_price=entry_price, exit_price=exit_price if status == "CLOSED" else None,
        status=status, exit_reason=exit_reason if status == "CLOSED" else None,
        opened_at=opened, closed_at=closed,
    )
    db.add(leg)
    return leg


@pytest.fixture()
def strategy(db_session):
    return _make_strategy(db_session)


class TestFillDivergenceEndpoint:
    def test_splits_legs_by_mode(self, db_session, strategy):
        for _ in range(5):
            _make_leg(db_session, strategy.id, "paper", 100, 80)
        for _ in range(5):
            _make_leg(db_session, strategy.id, "live", 100, 90)
        db_session.commit()

        result = get_fill_divergence(strategy.id, db_session, USER)
        assert result["paper"]["legs_closed"] == 5
        assert result["live"]["legs_closed"] == 5
        assert result["paper"]["avg_pnl_per_unit"] == 20.0
        assert result["live"]["avg_pnl_per_unit"] == 10.0
        assert result["avg_pnl_per_unit_diff"] == -10.0
        assert result["comparable"] is True

    def test_ignores_other_strategies_legs(self, db_session, strategy):
        other = _make_strategy(db_session, name="Other")
        for _ in range(5):
            _make_leg(db_session, strategy.id, "paper", 100, 80)
        for _ in range(5):
            _make_leg(db_session, other.id, "paper", 100, 50)  # would skew the average if leaked in
        db_session.commit()

        result = get_fill_divergence(strategy.id, db_session, USER)
        assert result["paper"]["legs_closed"] == 5
        assert result["paper"]["avg_pnl_per_unit"] == 20.0

    def test_open_legs_excluded_from_closed_count(self, db_session, strategy):
        for _ in range(5):
            _make_leg(db_session, strategy.id, "paper", 100, 80)
        _make_leg(db_session, strategy.id, "paper", 100, None, status="OPEN")
        db_session.commit()

        result = get_fill_divergence(strategy.id, db_session, USER)
        assert result["paper"]["legs_closed"] == 5

    def test_below_threshold_flagged_not_comparable(self, db_session, strategy):
        _make_leg(db_session, strategy.id, "paper", 100, 80)
        _make_leg(db_session, strategy.id, "live", 100, 80)
        db_session.commit()

        result = get_fill_divergence(strategy.id, db_session, USER)
        assert result["comparable"] is False

    def test_no_positions_at_all_does_not_crash(self, db_session, strategy):
        result = get_fill_divergence(strategy.id, db_session, USER)
        assert result["paper"]["legs_closed"] == 0
        assert result["live"]["legs_closed"] == 0
        assert result["comparable"] is False

    def test_not_found_for_other_users_strategy(self, db_session, strategy):
        with pytest.raises(HTTPException) as exc_info:
            get_fill_divergence(strategy.id, db_session, OTHER_USER)
        assert exc_info.value.status_code == 404
