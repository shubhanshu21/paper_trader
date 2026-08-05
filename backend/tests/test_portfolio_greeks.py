"""
tests/test_portfolio_greeks.py — api/live_greeks.py::compute_portfolio_greeks,
the account-wide Greeks aggregation (net exposure across EVERY open leg
of EVERY active strategy a user owns, not just one strategy's own combined
Greeks) added as a "what's good for our project" frontend improvement —
see StrategiesView.tsx's existing per-strategy Greeks widget, which this
sits alongside/above.

Real automate_test MySQL schema (see tests/conftest.py), a fake broker
pair — no network, never the production DB.

live_greeks.py does `from db.engine import SessionLocal` at
module load time, binding its OWN copy of that name into
api.live_greeks's namespace — patching
db.engine.SessionLocal (as test_wallet_real_margin.py does for
wallet.py, which instead does a LOCAL `from db.engine import
get_session` import inside the function body, reading the name at call
time) would NOT reach it. Must patch api.live_greeks.SessionLocal
directly, the exact name this module actually calls.
"""
import json
from datetime import date, timedelta

import pytest

import api.live_greeks as live_greeks
from api.live_greeks import compute_portfolio_greeks
from db.models import CustomStrategy, CustomStrategyPosition

NEAR_EXPIRY = (date.today() + timedelta(days=14)).isoformat()


@pytest.fixture()
def session_factory(db_session_factory, monkeypatch):
    monkeypatch.setattr(live_greeks, "SessionLocal", db_session_factory)
    return db_session_factory


class FakeBroker:
    def __init__(self, ltp, future_key=None, future_ltp=None):
        self.ltp = ltp
        self.future_key = future_key
        self.future_ltp = future_ltp

    def get_ltp_batch(self, tokens):
        return {t: self.ltp.get(t) for t in tokens}

    def get_ltp(self, key):
        return self.future_ltp if key == self.future_key else None


def _make_strategy(session, **overrides):
    defaults = {
        "user_id": 1, "name": "S", "instrument_type": "INDEX", "strategy_type": "CUSTOM", "option_type": "BOTH",
        "symbols": json.dumps(["NIFTY"]), "status": "PAPER_TRADING",
    }
    defaults.update(overrides)
    s = CustomStrategy(**defaults)
    session.add(s)
    session.commit()
    session.refresh(s)
    return s


def _make_leg(strategy_id, instrument_key, strike, option_type, transaction_type, entry_price=100.0, quantity=50, status="OPEN"):
    return CustomStrategyPosition(
        strategy_id=strategy_id, leg_index=0, mode="paper", instrument_key=instrument_key,
        instrument_type="OPTION", option_type=option_type, strike=strike, expiry=NEAR_EXPIRY,
        transaction_type=transaction_type, quantity=quantity, entry_price=entry_price, status=status,
    )


class TestNoActiveStrategies:
    def test_returns_empty_state_not_none(self, session_factory):
        result = compute_portfolio_greeks(owner_user_id=1)
        assert result["net"] is None
        assert result["by_strategy"] == []
        assert "message" in result


class TestNoOpenLegs:
    def test_active_strategy_with_no_open_legs(self, session_factory):
        session = session_factory()
        _make_strategy(session)
        session.close()
        result = compute_portfolio_greeks(owner_user_id=1)
        assert result["net"] is None
        assert result["open_legs_count"] == 0


class TestAggregatesAcrossStrategies:
    def test_sums_net_greeks_across_two_strategies_and_reports_per_strategy_breakdown(self, session_factory, monkeypatch):
        session = session_factory()
        s1 = _make_strategy(session, name="Strangle A")
        s2 = _make_strategy(session, name="Strangle B")
        session.add_all([
            _make_leg(s1.id, "NSE_FO|CE1", 24000, "CE", "SELL", entry_price=100.0),
            _make_leg(s2.id, "NSE_FO|PE1", 23000, "PE", "SELL", entry_price=90.0),
        ])
        session.commit()
        session.close()

        broker = FakeBroker(
            ltp={"NSE_FO|CE1": 95.0, "NSE_FO|PE1": 85.0, "NSE_FUT|1": 23800.0},
            future_key="NSE_FUT|1", future_ltp=23800.0,
        )
        monkeypatch.setattr(
            "api.custom_strategy_scheduler._get_brokers",
            lambda: {"paper": broker, "live": broker},
        )
        monkeypatch.setattr(
            "api.custom_strategy_scheduler._is_leg_for_symbol",
            lambda key, sym: True,
        )
        monkeypatch.setattr(
            "utils.instrument_cache.InstrumentCache.resolve_nearest_future_key",
            lambda self, symbol: ("NSE_FUT|1", NEAR_EXPIRY),
        )

        result = compute_portfolio_greeks(owner_user_id=1)

        assert result["open_legs_count"] == 2
        assert result["net"] is not None
        # Both legs are SELL options with real Greeks solved (real broker prices differ from entry) -> non-zero net.
        assert result["net"]["delta"] != 0.0
        assert len(result["by_strategy"]) == 2
        names = {b["name"] for b in result["by_strategy"]}
        assert names == {"Strangle A", "Strangle B"}
        for b in result["by_strategy"]:
            assert b["open_legs"] == 1

    def test_scoped_to_the_requesting_users_own_strategies_only(self, session_factory, monkeypatch):
        session = session_factory()
        mine = _make_strategy(session, user_id=1, name="Mine")
        theirs = _make_strategy(session, user_id=2, name="Theirs")
        session.add_all([
            _make_leg(mine.id, "NSE_FO|CE1", 24000, "CE", "SELL"),
            _make_leg(theirs.id, "NSE_FO|PE1", 23000, "PE", "SELL"),
        ])
        session.commit()
        session.close()

        broker = FakeBroker(ltp={"NSE_FO|CE1": 95.0}, future_key="NSE_FUT|1", future_ltp=23800.0)
        monkeypatch.setattr(
            "api.custom_strategy_scheduler._get_brokers",
            lambda: {"paper": broker, "live": broker},
        )
        monkeypatch.setattr(
            "api.custom_strategy_scheduler._is_leg_for_symbol",
            lambda key, sym: True,
        )
        monkeypatch.setattr(
            "utils.instrument_cache.InstrumentCache.resolve_nearest_future_key",
            lambda self, symbol: ("NSE_FUT|1", NEAR_EXPIRY),
        )

        result = compute_portfolio_greeks(owner_user_id=1)
        assert result["open_legs_count"] == 1
        assert [b["name"] for b in result["by_strategy"]] == ["Mine"]

    def test_broker_not_ready_returns_message_not_error(self, session_factory, monkeypatch):
        session = session_factory()
        s = _make_strategy(session)
        session.add(_make_leg(s.id, "NSE_FO|CE1", 24000, "CE", "SELL"))
        session.commit()
        session.close()

        monkeypatch.setattr("api.custom_strategy_scheduler._get_brokers", lambda: None)

        result = compute_portfolio_greeks(owner_user_id=1)
        assert result["net"] is None
        assert "not ready" in result["message"].lower()
