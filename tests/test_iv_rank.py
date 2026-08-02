"""
tests/test_iv_rank.py — utils/iv_rank.py: IV-rank computation off
symbol_iv_history (see api/iv_history_scheduler.py, the only writer).
Direct-function-call style against the shared automate_test MySQL schema
(see tests/conftest.py), patching utils.iv_rank.SessionLocal to a
sessionmaker bound to the test's own transaction (a fresh session per
call, matching this session's established SessionLocal-mocking pattern —
see tests/test_routes_backtest_runs.py).
"""
from datetime import date, timedelta
from unittest.mock import patch

from automate.db.models import SymbolIvHistory
from automate.utils import iv_rank


def _seed(Session, symbol: str, ivs_oldest_first):
    session = Session()
    today = date.today()
    for i, iv in enumerate(ivs_oldest_first):
        trade_date = (today - timedelta(days=len(ivs_oldest_first) - i)).isoformat()
        session.add(SymbolIvHistory(symbol=symbol, trade_date=trade_date, atm_iv=iv))
    session.commit()
    session.close()


class TestComputeIvRank:
    def test_below_minimum_history_returns_none(self, db_session_factory):
        _seed(db_session_factory, "NIFTY", [20.0] * 10)  # fewer than _MIN_HISTORY_DAYS=30
        with patch.object(iv_rank, "SessionLocal", db_session_factory):
            assert iv_rank.compute_iv_rank("NIFTY") is None

    def test_ranks_current_iv_within_trailing_window(self, db_session_factory):
        ivs = [10.0 + i for i in range(40)]  # 10..49, most recent = 49
        _seed(db_session_factory, "NIFTY", ivs)
        with patch.object(iv_rank, "SessionLocal", db_session_factory):
            rank = iv_rank.compute_iv_rank("NIFTY")
        assert rank == 100.0  # most recent stored row (49) is the window's own high

    def test_explicit_today_iv_overrides_the_most_recent_stored_row(self, db_session_factory):
        ivs = [10.0 + i for i in range(40)]  # range 10..49
        _seed(db_session_factory, "NIFTY", ivs)
        with patch.object(iv_rank, "SessionLocal", db_session_factory):
            rank = iv_rank.compute_iv_rank("NIFTY", today_iv=10.0)  # the window's own low
        assert rank == 0.0

    def test_degenerate_zero_variance_window_returns_50(self, db_session_factory):
        _seed(db_session_factory, "NIFTY", [15.0] * 35)
        with patch.object(iv_rank, "SessionLocal", db_session_factory):
            assert iv_rank.compute_iv_rank("NIFTY") == 50.0

    def test_never_fabricates_a_value_for_an_unknown_symbol(self, db_session_factory):
        _seed(db_session_factory, "NIFTY", [20.0] * 40)
        with patch.object(iv_rank, "SessionLocal", db_session_factory):
            assert iv_rank.compute_iv_rank("BANKNIFTY") is None


class TestHistorySufficiency:
    def test_reports_days_and_sufficiency_flag(self, db_session_factory):
        _seed(db_session_factory, "NIFTY", [20.0] * 10)
        with patch.object(iv_rank, "SessionLocal", db_session_factory):
            info = iv_rank.history_sufficiency("NIFTY")
        assert info == {"days": 10, "required": 30, "sufficient": False}

    def test_sufficient_once_min_history_days_reached(self, db_session_factory):
        _seed(db_session_factory, "NIFTY", [20.0] * 30)
        with patch.object(iv_rank, "SessionLocal", db_session_factory):
            info = iv_rank.history_sufficiency("NIFTY")
        assert info["sufficient"] is True
