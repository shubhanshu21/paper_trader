"""
tests/test_equity_paper_close_pnl.py — routes_equity.py::close_equity_position,
covering a real bug: manually closing a paper equity position always used
entry_price AS the exit price, so gross_pnl was mathematically always 0
regardless of actual price movement. _resolve_paper_exit_price now tries
a live LTP from the paper broker first, falls back to the last poller-
written current_price, and only falls back to entry_price (an honest 0
P&L, not a fabricated one) if neither is available.

Real automate_test MySQL schema (see tests/conftest.py) — no network.
"""
from contextlib import contextmanager
from datetime import date

import pytest

import api.deps as deps
import api.routes_equity as routes
from api.routes_equity import _resolve_paper_exit_price, close_equity_position
from db.models import EquityPosition

USER = {"sub": "1"}


@pytest.fixture()
def db(db_session_factory, monkeypatch):
    @contextmanager
    def fake_get_session():
        session = db_session_factory()
        try:
            yield session
            session.commit()
        finally:
            session.close()

    monkeypatch.setattr(routes, "get_session", fake_get_session)
    return db_session_factory()


def _make_open_position(db, **overrides):
    defaults = {
        "user_id": 1, "strategy_name": "equity_ma_crossover", "mode": "paper", "symbol": "NSE_EQ|RELIANCE",
        "direction": "LONG", "product": "CNC", "entry_date": date.today().isoformat(),
        "entry_price": 100.0, "quantity": 10, "status": "OPEN",
    }
    defaults.update(overrides)
    pos = EquityPosition(**defaults)
    db.add(pos)
    db.commit()
    db.refresh(pos)
    return pos


class FakePaperBroker:
    def __init__(self, ltp=None):
        self._ltp = ltp

    def get_ltp(self, instrument_key):
        return self._ltp


class TestResolvePaperExitPrice:
    def test_uses_live_ltp_when_broker_available(self, db, monkeypatch):
        pos = _make_open_position(db)
        monkeypatch.setattr(deps, "get_brokers", lambda: {"paper": FakePaperBroker(ltp=120.0)})
        assert _resolve_paper_exit_price(pos) == 120.0

    def test_falls_back_to_current_price_when_broker_has_no_ltp(self, db, monkeypatch):
        pos = _make_open_position(db, current_price=110.0)
        monkeypatch.setattr(deps, "get_brokers", lambda: {"paper": FakePaperBroker(ltp=None)})
        assert _resolve_paper_exit_price(pos) == 110.0

    def test_falls_back_to_entry_price_when_nothing_else_available(self, db, monkeypatch):
        pos = _make_open_position(db)
        monkeypatch.setattr(deps, "get_brokers", lambda: {"paper": FakePaperBroker(ltp=None)})
        assert _resolve_paper_exit_price(pos) == 100.0

    def test_broker_lookup_failure_falls_back_gracefully(self, db, monkeypatch):
        pos = _make_open_position(db, current_price=115.0)

        def _raise():
            raise RuntimeError("broker down")
        monkeypatch.setattr(deps, "get_brokers", _raise)
        assert _resolve_paper_exit_price(pos) == 115.0


class TestCloseEquityPositionPnl:
    def test_manual_close_reports_real_pnl_not_zero(self, db, monkeypatch):
        pos = _make_open_position(db, entry_price=100.0, quantity=10, direction="LONG")
        monkeypatch.setattr(deps, "get_brokers", lambda: {"paper": FakePaperBroker(ltp=130.0)})

        result = close_equity_position(pos.id, USER)

        assert result["status"] == "CLOSED"
        assert result["exit_price"] == 130.0
        assert float(result["gross_pnl"]) == pytest.approx(300.0)  # (130-100)*10

    def test_short_direction_inverts_pnl(self, db, monkeypatch):
        pos = _make_open_position(db, entry_price=100.0, quantity=10, direction="SHORT")
        monkeypatch.setattr(deps, "get_brokers", lambda: {"paper": FakePaperBroker(ltp=80.0)})

        result = close_equity_position(pos.id, USER)

        assert float(result["gross_pnl"]) == pytest.approx(200.0)  # price fell 20 * qty 10, SHORT profits
