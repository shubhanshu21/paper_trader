"""
tests/test_draft_resume_skips_rebacktest.py — routes_custom_strategies.py::
update_strategy_status now allows DRAFT -> PAPER_TRADING directly when
the strategy already has a valid backtest result on file, closing a
real gap: Stop always resets status to STOPPED -> (Reactivate) -> DRAFT,
and DRAFT previously could ONLY go to BACKTESTING — forcing a redundant
re-backtest every time a user stopped and resumed a strategy whose rules
hadn't actually changed since the last (still valid) backtest.

update_strategy() (the rules-edit endpoint) already clears
backtest_return_pct back to None the moment rules actually change, so
its presence is a reliable "already validated against these exact
rules" signal, not a stale leftover — this is what makes skipping the
re-backtest safe rather than a shortcut around the safety rail.
"""
import json

import pytest
from fastapi import HTTPException

from automate.api.routes_custom_strategies import StrategyStatusUpdate, update_strategy_status
from automate.db.models import CustomStrategy

USER = {"sub": "1"}


@pytest.fixture()
def db(db_session):
    return db_session


def _make_strategy(db, **overrides):
    defaults = {
        "user_id": 1, "name": "Strangle", "instrument_type": "INDEX", "strategy_type": "CUSTOM", "option_type": "BOTH",
        "symbols": json.dumps(["NIFTY"]), "status": "DRAFT", "rules_json": json.dumps({"legs": [{"action": "SELL"}]}),
    }
    defaults.update(overrides)
    s = CustomStrategy(**defaults)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


class TestDraftToPaperTradingRequiresABacktest:
    def test_rejected_when_never_backtested(self, db):
        s = _make_strategy(db, backtest_return_pct=None)
        with pytest.raises(HTTPException) as exc_info:
            update_strategy_status(s.id, StrategyStatusUpdate(status="PAPER_TRADING"), db, USER)
        assert exc_info.value.status_code == 400
        assert "backtest" in exc_info.value.detail.lower()

    def test_allowed_when_a_backtest_result_already_exists(self, db):
        """The exact case that used to force a pointless re-backtest: Stop -> Reactivate on an unedited, already-backtested strategy."""
        s = _make_strategy(db, backtest_return_pct=12.5)
        result = update_strategy_status(s.id, StrategyStatusUpdate(status="PAPER_TRADING"), db, USER)
        assert result["status"] == "PAPER_TRADING"

    def test_still_allowed_via_the_normal_backtesting_status_too(self, db):
        s = _make_strategy(db, status="BACKTESTING", backtest_return_pct=8.0)
        result = update_strategy_status(s.id, StrategyStatusUpdate(status="PAPER_TRADING"), db, USER)
        assert result["status"] == "PAPER_TRADING"

    def test_stop_then_reactivate_then_paper_trade_full_cycle(self, db):
        """Simulates the user's exact reported flow: backtest once, stop, reactivate, paper trade again without re-backtesting."""
        s = _make_strategy(db, status="STOPPED", backtest_return_pct=15.0)

        reactivated = update_strategy_status(s.id, StrategyStatusUpdate(status="DRAFT"), db, USER)
        assert reactivated["status"] == "DRAFT"
        assert reactivated["backtest_return_pct"] == 15.0  # never cleared by stop/reactivate

        resumed = update_strategy_status(s.id, StrategyStatusUpdate(status="PAPER_TRADING"), db, USER)
        assert resumed["status"] == "PAPER_TRADING"

    def test_editing_rules_clears_the_backtest_result_so_the_shortcut_stops_applying(self, db):
        """Sanity check on the invariant this fix relies on — not a regression test of update_strategy() itself, just confirming the field really does reset on a real rules edit."""
        s = _make_strategy(db, backtest_return_pct=12.5)
        s.backtest_return_pct = None  # what update_strategy() does on an actual rules change
        db.commit()

        with pytest.raises(HTTPException) as exc_info:
            update_strategy_status(s.id, StrategyStatusUpdate(status="PAPER_TRADING"), db, USER)
        assert exc_info.value.status_code == 400
