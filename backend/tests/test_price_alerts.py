"""
tests/test_price_alerts.py — api/routes_price_alerts.py (CRUD) and
api/price_alert_scheduler.py (live-LTP evaluation).

Both modules do a module-level `from db.engine import SessionLocal`
(same pattern as routes_leaderboard.py/routes_performance.py) — the
autouse _never_touch_real_notifications fixture in conftest.py only
patches db.engine.SessionLocal itself, which does NOT retroactively
redirect an already-bound module-level name. Each test class here
patches SessionLocal directly on the module under test instead, so these
tests never touch the real production DB.
"""
from datetime import datetime

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from api.price_alert_scheduler import _condition_met, _run_once
from api.routes_price_alerts import CreateAlertRequest, create_alert, delete_alert, list_alerts
from db.models import PriceAlert


@pytest.fixture()
def db(db_session):
    return db_session


@pytest.fixture(autouse=True)
def _redirect_module_sessions(db_session_factory, monkeypatch):
    monkeypatch.setattr("api.routes_price_alerts.SessionLocal", db_session_factory)
    monkeypatch.setattr("api.price_alert_scheduler.SessionLocal", db_session_factory)


class TestConditionMet:
    def test_above_fires_when_current_exceeds_target(self):
        assert _condition_met("ABOVE", current=105, target=100, last_seen=None) is True

    def test_above_does_not_fire_when_current_is_below_target(self):
        assert _condition_met("ABOVE", current=95, target=100, last_seen=None) is False

    def test_below_fires_when_current_is_under_target(self):
        assert _condition_met("BELOW", current=95, target=100, last_seen=None) is True

    def test_crosses_above_fires_only_on_the_actual_crossing(self):
        assert _condition_met("CROSSES_ABOVE", current=105, target=100, last_seen=95) is True
        # already above last tick too — must NOT keep re-firing every tick
        assert _condition_met("CROSSES_ABOVE", current=105, target=100, last_seen=101) is False

    def test_crosses_above_with_no_prior_price_never_fires(self):
        # first-ever tick for this alert — nothing to compare against yet
        assert _condition_met("CROSSES_ABOVE", current=105, target=100, last_seen=None) is False

    def test_crosses_below_fires_only_on_the_actual_crossing(self):
        assert _condition_met("CROSSES_BELOW", current=95, target=100, last_seen=105) is True
        assert _condition_met("CROSSES_BELOW", current=95, target=100, last_seen=99) is False


class FakeBroker:
    def __init__(self, prices: dict):
        self._prices = prices

    def resolve_instrument_key(self, symbol: str) -> str:
        return f"KEY|{symbol}"

    def get_ltp(self, instrument_key: str) -> float | None:
        symbol = instrument_key.split("|", 1)[1]
        return self._prices.get(symbol)


class TestRunOnce:
    def _make_alert(self, db, **overrides):
        defaults = {"user_id": 1, "symbol": "NIFTY", "condition": "ABOVE", "target_price": 24000.0, "status": "ACTIVE"}
        defaults.update(overrides)
        a = PriceAlert(**defaults)
        db.add(a)
        db.commit()
        db.refresh(a)
        return a

    def test_no_brokers_is_a_silent_noop(self, monkeypatch):
        monkeypatch.setattr("api.price_alert_scheduler._get_brokers", lambda: None)
        _run_once()  # must not raise

    def test_triggers_and_notifies_when_condition_met(self, db, monkeypatch):
        monkeypatch.setattr("api.price_alert_scheduler._get_brokers", lambda: {"paper": FakeBroker({"NIFTY": 24500.0})})
        notified = []
        monkeypatch.setattr("api.price_alert_scheduler.notify", lambda *a, **k: notified.append((a, k)))

        alert = self._make_alert(db)
        _run_once()
        db.refresh(alert)

        assert alert.status == "TRIGGERED"
        assert float(alert.triggered_price) == 24500.0
        assert alert.triggered_at is not None
        assert len(notified) == 1
        assert "NIFTY" in notified[0][0][1]

    def test_does_not_trigger_when_condition_not_met(self, db, monkeypatch):
        monkeypatch.setattr("api.price_alert_scheduler._get_brokers", lambda: {"paper": FakeBroker({"NIFTY": 23000.0})})
        monkeypatch.setattr("api.price_alert_scheduler.notify", lambda *a, **k: None)

        alert = self._make_alert(db)
        _run_once()
        db.refresh(alert)

        assert alert.status == "ACTIVE"
        assert float(alert.last_seen_price) == 23000.0  # remembered for a future CROSSES_* check

    def test_missing_ltp_leaves_alert_untouched(self, db, monkeypatch):
        monkeypatch.setattr("api.price_alert_scheduler._get_brokers", lambda: {"paper": FakeBroker({})})
        monkeypatch.setattr("api.price_alert_scheduler.notify", lambda *a, **k: None)

        alert = self._make_alert(db)
        _run_once()
        db.refresh(alert)

        assert alert.status == "ACTIVE"
        assert alert.last_seen_price is None

    def test_triggered_alerts_are_never_re_evaluated(self, db, monkeypatch):
        monkeypatch.setattr("api.price_alert_scheduler._get_brokers", lambda: {"paper": FakeBroker({"NIFTY": 24500.0})})
        calls = []
        monkeypatch.setattr("api.price_alert_scheduler.notify", lambda *a, **k: calls.append(1))

        self._make_alert(db, status="TRIGGERED", triggered_at=datetime.now(), triggered_price=24000.0)
        _run_once()
        assert calls == []


class TestRoutes:
    def test_create_then_list_then_delete(self, db):
        user = {"sub": "1"}
        created = create_alert(CreateAlertRequest(symbol="nifty", condition="above", target_price=25000), user)
        assert created["symbol"] == "NIFTY"  # uppercased
        assert created["condition"] == "ABOVE"
        assert created["status"] == "ACTIVE"

        listed = list_alerts(status=None, user=user)
        assert len(listed["alerts"]) == 1
        assert listed["alerts"][0]["id"] == created["id"]

        result = delete_alert(created["id"], user)
        assert result == {"status": "deleted"}
        assert list_alerts(status=None, user=user)["alerts"] == []

    def test_cannot_delete_another_users_alert(self, db):
        created = create_alert(CreateAlertRequest(symbol="NIFTY", condition="above", target_price=25000), {"sub": "1"})
        with pytest.raises(HTTPException) as exc_info:
            delete_alert(created["id"], {"sub": "2"})
        assert exc_info.value.status_code == 404

    def test_status_filter(self, db):
        user = {"sub": "1"}
        create_alert(CreateAlertRequest(symbol="NIFTY", condition="above", target_price=25000), user)
        a2 = PriceAlert(user_id=1, symbol="BANKNIFTY", condition="BELOW", target_price=50000, status="TRIGGERED")
        db.add(a2)
        db.commit()

        assert len(list_alerts(status="active", user=user)["alerts"]) == 1
        assert len(list_alerts(status="triggered", user=user)["alerts"]) == 1
        assert len(list_alerts(status=None, user=user)["alerts"]) == 2

    def test_invalid_condition_is_rejected(self):
        with pytest.raises(ValidationError):
            CreateAlertRequest(symbol="NIFTY", condition="SIDEWAYS", target_price=25000)

    def test_non_positive_target_price_is_rejected(self):
        with pytest.raises(ValidationError):
            CreateAlertRequest(symbol="NIFTY", condition="above", target_price=0)
