"""
tests/test_iv_rank.py — utils/iv_rank.py: IV-rank computation off
symbol_iv_history (see api/iv_history_scheduler.py, the only writer).
Direct-function-call style against an in-memory SQLite DB, patching
utils.iv_rank.SessionLocal to a sessionmaker bound to it (a fresh session
per call, matching this session's established SessionLocal-mocking
pattern — see tests/test_routes_backtest_runs.py).
"""
from datetime import date, timedelta
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.types import BigInteger


@compiles(BigInteger, "sqlite")
def compile_bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"


from automate.db.engine import Base
from automate.db.models import SymbolIvHistory
from automate.utils import iv_rank


def _sqlite_sessionmaker():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[SymbolIvHistory.__table__])
    return sessionmaker(bind=engine)


def _seed(Session, symbol: str, ivs_oldest_first):
    session = Session()
    today = date.today()
    for i, iv in enumerate(ivs_oldest_first):
        trade_date = (today - timedelta(days=len(ivs_oldest_first) - i)).isoformat()
        session.add(SymbolIvHistory(symbol=symbol, trade_date=trade_date, atm_iv=iv))
    session.commit()
    session.close()


class TestComputeIvRank:
    def test_below_minimum_history_returns_none(self):
        Session = _sqlite_sessionmaker()
        _seed(Session, "NIFTY", [20.0] * 10)  # fewer than _MIN_HISTORY_DAYS=30
        with patch.object(iv_rank, "SessionLocal", Session):
            assert iv_rank.compute_iv_rank("NIFTY") is None

    def test_ranks_current_iv_within_trailing_window(self):
        Session = _sqlite_sessionmaker()
        ivs = [10.0 + i for i in range(40)]  # 10..49, most recent = 49
        _seed(Session, "NIFTY", ivs)
        with patch.object(iv_rank, "SessionLocal", Session):
            rank = iv_rank.compute_iv_rank("NIFTY")
        assert rank == 100.0  # most recent stored row (49) is the window's own high

    def test_explicit_today_iv_overrides_the_most_recent_stored_row(self):
        Session = _sqlite_sessionmaker()
        ivs = [10.0 + i for i in range(40)]  # range 10..49
        _seed(Session, "NIFTY", ivs)
        with patch.object(iv_rank, "SessionLocal", Session):
            rank = iv_rank.compute_iv_rank("NIFTY", today_iv=10.0)  # the window's own low
        assert rank == 0.0

    def test_degenerate_zero_variance_window_returns_50(self):
        Session = _sqlite_sessionmaker()
        _seed(Session, "NIFTY", [15.0] * 35)
        with patch.object(iv_rank, "SessionLocal", Session):
            assert iv_rank.compute_iv_rank("NIFTY") == 50.0

    def test_never_fabricates_a_value_for_an_unknown_symbol(self):
        Session = _sqlite_sessionmaker()
        _seed(Session, "NIFTY", [20.0] * 40)
        with patch.object(iv_rank, "SessionLocal", Session):
            assert iv_rank.compute_iv_rank("BANKNIFTY") is None


class TestHistorySufficiency:
    def test_reports_days_and_sufficiency_flag(self):
        Session = _sqlite_sessionmaker()
        _seed(Session, "NIFTY", [20.0] * 10)
        with patch.object(iv_rank, "SessionLocal", Session):
            info = iv_rank.history_sufficiency("NIFTY")
        assert info == {"days": 10, "required": 30, "sufficient": False}

    def test_sufficient_once_min_history_days_reached(self):
        Session = _sqlite_sessionmaker()
        _seed(Session, "NIFTY", [20.0] * 30)
        with patch.object(iv_rank, "SessionLocal", Session):
            info = iv_rank.history_sufficiency("NIFTY")
        assert info["sufficient"] is True
