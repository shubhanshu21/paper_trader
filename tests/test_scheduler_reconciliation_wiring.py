"""
tests/test_scheduler_reconciliation_wiring.py — custom_strategy_scheduler.py::
_reconcile_live_positions, the caller that wires utils/position_reconciliation.py's
pure diff logic to a real DB session + broker + notify() alerting. _reconcile_live_positions
takes its `db` session as a plain argument (no module-level SessionLocal to
monkeypatch) — an in-memory SQLite session is passed straight in.
"""
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.types import BigInteger


@compiles(BigInteger, "sqlite")
def compile_bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"


from automate.db.engine import Base
from automate.db.models import CustomStrategy, CustomStrategyPosition
from automate.api.custom_strategy_scheduler import _reconcile_live_positions


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[CustomStrategy.__table__, CustomStrategyPosition.__table__])
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    yield session
    session.close()


def _make_strategy(db, **overrides):
    defaults = dict(
        user_id=1, name="Live Strangle", instrument_type="INDEX", strategy_type="CUSTOM", option_type="BOTH",
        symbols=json.dumps(["NIFTY"]), status="LIVE",
    )
    defaults.update(overrides)
    s = CustomStrategy(**defaults)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _make_leg(strategy_id, instrument_key, transaction_type="SELL", quantity=50, mode="live", status="OPEN"):
    return CustomStrategyPosition(
        strategy_id=strategy_id, leg_index=0, mode=mode, instrument_key=instrument_key,
        instrument_type="OPTION", option_type="CE", strike=24000, expiry="2026-08-27",
        transaction_type=transaction_type, quantity=quantity, entry_price=100.0, status=status,
    )


class FakeLiveBroker:
    def __init__(self, positions):
        self._positions = positions

    def get_broker_positions(self):
        return self._positions


class TestReconcileLivePositions:
    def test_no_live_broker_is_a_silent_noop(self, db, monkeypatch):
        notified = []
        monkeypatch.setattr("automate.utils.notify.notify", lambda *a, **k: notified.append((a, k)))
        _reconcile_live_positions(db, {"paper": object()})  # no "live" key at all
        assert notified == []

    def test_no_open_live_legs_is_a_silent_noop(self, db, monkeypatch):
        notified = []
        monkeypatch.setattr("automate.utils.notify.notify", lambda *a, **k: notified.append((a, k)))
        _reconcile_live_positions(db, {"live": FakeLiveBroker({})})
        assert notified == []

    def test_matching_positions_send_no_alert(self, db, monkeypatch):
        s = _make_strategy(db)
        db.add(_make_leg(s.id, "NSE_FO|CE1", "SELL", 50))
        db.commit()
        notified = []
        monkeypatch.setattr("automate.api.custom_strategy_scheduler.notify", lambda *a, **k: notified.append((a, k)))
        _reconcile_live_positions(db, {"live": FakeLiveBroker({"NSE_FO|CE1": -50})})
        assert notified == []

    def test_mismatch_triggers_an_alert_scoped_to_the_owning_user(self, db, monkeypatch):
        s = _make_strategy(db, user_id=7)
        db.add(_make_leg(s.id, "NSE_FO|CE1", "SELL", 50))
        db.commit()
        notified = []
        monkeypatch.setattr("automate.api.custom_strategy_scheduler.notify", lambda *a, **k: notified.append((a, k)))
        # Broker shows FLAT (0) — DB expects -50. A real mismatch.
        _reconcile_live_positions(db, {"live": FakeLiveBroker({})})

        assert len(notified) == 1
        args, kwargs = notified[0]
        assert kwargs["user_id"] == 7
        assert kwargs["level"] == "error"
        assert "NSE_FO|CE1" in args[1]

    def test_broker_positions_call_failing_does_not_send_a_false_all_clear(self, db, monkeypatch):
        s = _make_strategy(db)
        db.add(_make_leg(s.id, "NSE_FO|CE1", "SELL", 50))
        db.commit()
        notified = []
        monkeypatch.setattr("automate.api.custom_strategy_scheduler.notify", lambda *a, **k: notified.append((a, k)))
        _reconcile_live_positions(db, {"live": FakeLiveBroker(None)})  # broker call failed
        assert notified == []  # "couldn't check" must never look like "all clear"

    def test_paper_legs_are_never_included_in_the_comparison(self, db, monkeypatch):
        s = _make_strategy(db, status="PAPER_TRADING")
        db.add(_make_leg(s.id, "NSE_FO|CE1", "SELL", 50, mode="paper"))
        db.commit()
        notified = []
        monkeypatch.setattr("automate.api.custom_strategy_scheduler.notify", lambda *a, **k: notified.append((a, k)))
        _reconcile_live_positions(db, {"live": FakeLiveBroker({})})
        assert notified == []  # no OPEN mode='live' legs at all -> early-return no-op
