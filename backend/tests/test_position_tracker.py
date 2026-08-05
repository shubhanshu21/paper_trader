"""
tests/test_position_tracker.py — Tests for utils/position_tracker.py.
Uses the shared automate_test MySQL schema (see tests/conftest.py) via
the mock_db fixture — no shared state with the production database, no
network.
"""
from contextlib import contextmanager

import pytest

import utils.position_tracker
from utils.position_tracker import (
    close_position,
    get_open_positions,
    has_open_position,
    record_open_position,
)


@pytest.fixture(autouse=True)
def mock_db(db_session_factory):
    @contextmanager
    def mock_get_session():
        session = db_session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    original_get_session = utils.position_tracker.get_session
    utils.position_tracker.get_session = mock_get_session
    yield
    utils.position_tracker.get_session = original_get_session


def _record(**overrides):
    defaults = {
        "strategy_name": "ten_percent_otm_strangle", "mode": "paper", "symbol": "RELIANCE",
        "entry_date": "2026-01-01", "expiry": "2026-01-29",
        "call_token": "BHAV|RELIANCE|2026-01-29|1700|CE", "call_strike": 1700, "call_entry_price": 5.0, "call_order_id": "ORD-CE",
        "put_token": "BHAV|RELIANCE|2026-01-29|1400|PE", "put_strike": 1400, "put_entry_price": 4.0, "put_order_id": "ORD-PE",
        "quantity": 500, "product": "NRML", "take_profit_pct": 60.0, "stop_loss_pct": 150.0,
        "exit_days_before_expiry": 1,
    }
    defaults.update(overrides)
    return record_open_position(**defaults)


class TestRecordAndGetOpenPositions:
    def test_recorded_position_appears_as_open(self):
        pid = _record()
        open_positions = get_open_positions()
        assert len(open_positions) == 1
        assert open_positions[0]["id"] == pid
        assert open_positions[0]["symbol"] == "RELIANCE"
        assert open_positions[0]["status"] == "OPEN"

    def test_filter_by_strategy_name(self):
        _record(strategy_name="strategy_a")
        _record(strategy_name="strategy_b")
        assert len(get_open_positions()) == 2
        assert len(get_open_positions(strategy_name="strategy_a")) == 1

    def test_no_positions_returns_empty_list(self):
        assert get_open_positions() == []


class TestClosePosition:
    def test_closed_position_no_longer_open(self):
        pid = _record()
        close_position(
            pid, exit_date="2026-01-10", exit_reason="STOP_LOSS",
            call_exit_price=8.0, put_exit_price=6.0,
            call_exit_order_id="EXIT-CE", put_exit_order_id="EXIT-PE",
        )
        assert get_open_positions() == []

    def test_closing_one_leaves_others_open(self):
        pid1 = _record()
        _record()
        close_position(pid1, "2026-01-10", "TAKE_PROFIT", 1.0, 1.0, "EXIT-CE", "EXIT-PE")
        remaining = get_open_positions()
        assert len(remaining) == 1
        assert remaining[0]["id"] != pid1


class TestHasOpenPosition:
    """Entry-gating: one cycle at a time per (strategy, symbol) — see
    run_strategy.py's run_entries(), which skips entry when this is True."""

    def test_false_when_nothing_open(self):
        assert has_open_position("ten_percent_otm_strangle", "RELIANCE") is False

    def test_true_once_a_position_is_open(self):
        _record(strategy_name="ten_percent_otm_strangle", symbol="RELIANCE")
        assert has_open_position("ten_percent_otm_strangle", "RELIANCE") is True

    def test_false_again_after_closing(self):
        pid = _record(strategy_name="ten_percent_otm_strangle", symbol="RELIANCE")
        close_position(pid, "2026-01-10", "EXPIRY", 1.0, 1.0, "EXIT-CE", "EXIT-PE")
        assert has_open_position("ten_percent_otm_strangle", "RELIANCE") is False

    def test_scoped_to_symbol_and_strategy(self):
        _record(strategy_name="ten_percent_otm_strangle", symbol="RELIANCE")
        assert has_open_position("ten_percent_otm_strangle", "TCS") is False
        assert has_open_position("some_other_strategy", "RELIANCE") is False
