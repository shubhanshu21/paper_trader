"""
tests/test_paused_strategy_still_exit_managed.py — custom_strategy_scheduler.py::
_tick_one_strategy, covering the PAUSED case added to close a real gap:
pausing a strategy previously excluded it from the scheduler entirely
(same query as STOPPED), silently abandoning any already-open real
position with zero further TP/SL/trailing/time-exit management until the
strategy was resumed or stopped. PAUSE is meant to be reversible (unlike
STOP, which now square-offs — see test_stop_squares_off_positions.py) so
the fix here is the opposite: keep running _try_exit for a paused
strategy's already-open legs, using the leg's OWN recorded mode (not the
now-ambiguous PAUSED status) to pick the broker, while skipping
_try_entry entirely (no new positions while paused).
"""
import json

from api.custom_strategy_scheduler import _tick_one_strategy
from db.models import CustomStrategy, CustomStrategyPosition


def _make_strategy(db, **overrides):
    defaults = {
        "user_id": 1, "name": "Strangle", "instrument_type": "INDEX", "strategy_type": "CUSTOM", "option_type": "BOTH",
        "symbols": json.dumps(["NIFTY"]), "status": "PAUSED",
        "rules_json": json.dumps({"legs": [{"action": "SELL"}]}),
    }
    defaults.update(overrides)
    s = CustomStrategy(**defaults)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _make_leg(strategy_id, instrument_key="NSE_FO|1", mode="live", status="OPEN"):
    return CustomStrategyPosition(
        strategy_id=strategy_id, leg_index=0, mode=mode, instrument_key=instrument_key,
        instrument_type="OPTION", option_type="CE", strike=24000, expiry="2026-08-27",
        transaction_type="SELL", quantity=50, entry_price=100.0, status=status,
    )


class FakeBroker:
    def __init__(self, ltp=100.0):
        self.ltp = ltp
        self.entry_calls = 0
        self.orders_placed = []

    def get_ltp_batch(self, tokens):
        return dict.fromkeys(tokens, self.ltp)

    def place_buy_order(self, instrument_token, quantity, order_type, tag, user_id=None, product="NRML", is_close=False):
        self.orders_placed.append(instrument_token)
        return "ORD1"

    def place_sell_order(self, instrument_token, quantity, order_type, tag, user_id=None, product="NRML", is_close=False):
        self.orders_placed.append(instrument_token)
        return "ORD1"

    def get_fill_price(self, order_id):
        return None


class TestPausedStrategyTick:
    def test_no_open_legs_is_a_pure_noop(self, db_session, monkeypatch):
        s = _make_strategy(db_session)
        live_broker = FakeBroker()
        _tick_one_strategy(db_session, s, {"live": live_broker, "paper": FakeBroker()})
        assert live_broker.orders_placed == []

    def test_open_leg_still_gets_exit_checked_using_its_own_mode(self, db_session, monkeypatch):
        s = _make_strategy(db_session)
        leg = _make_leg(s.id, mode="live")
        db_session.add(leg)
        db_session.commit()

        monkeypatch.setattr(
            "api.custom_strategy_scheduler._is_leg_for_symbol", lambda key, sym: True,
        )
        live_broker = FakeBroker(ltp=1.0)  # SELL entry@100 -> huge profit at 1.0, no exit config -> combined check needs strategy exit rule
        s.rules_json = json.dumps({
            "legs": [{"action": "SELL"}],
            "exit": {"take_profit_pct": 10.0},
        })
        db_session.commit()

        _tick_one_strategy(db_session, s, {"live": live_broker, "paper": FakeBroker()})

        db_session.refresh(leg)
        assert leg.status == "CLOSED"
        assert live_broker.orders_placed == ["NSE_FO|1"]

    def test_never_enters_a_new_position_while_paused(self, db_session, monkeypatch):
        s = _make_strategy(db_session)
        leg = _make_leg(s.id, mode="paper")
        db_session.add(leg)
        db_session.commit()

        called = {"entry": False}
        monkeypatch.setattr(
            "api.custom_strategy_scheduler._try_entry",
            lambda *a, **k: called.__setitem__("entry", True),
        )
        monkeypatch.setattr(
            "api.custom_strategy_scheduler._is_leg_for_symbol", lambda key, sym: True,
        )
        paper_broker = FakeBroker(ltp=100.0)  # flat — no exit trigger

        _tick_one_strategy(db_session, s, {"live": FakeBroker(), "paper": paper_broker})

        assert called["entry"] is False

    def test_picks_the_paper_broker_when_the_open_leg_is_paper_mode(self, db_session, monkeypatch):
        s = _make_strategy(db_session)
        leg = _make_leg(s.id, mode="paper")
        db_session.add(leg)
        db_session.commit()
        s.rules_json = json.dumps({"legs": [{"action": "SELL"}], "exit": {"take_profit_pct": 10.0}})
        db_session.commit()

        monkeypatch.setattr(
            "api.custom_strategy_scheduler._is_leg_for_symbol", lambda key, sym: True,
        )
        live_broker = FakeBroker(ltp=1.0)
        paper_broker = FakeBroker(ltp=1.0)

        _tick_one_strategy(db_session, s, {"live": live_broker, "paper": paper_broker})

        assert live_broker.orders_placed == []
        assert paper_broker.orders_placed == ["NSE_FO|1"]

    def test_no_rules_json_is_a_noop(self, db_session):
        s = _make_strategy(db_session, rules_json=None)
        live_broker = FakeBroker()
        _tick_one_strategy(db_session, s, {"live": live_broker, "paper": FakeBroker()})
        assert live_broker.orders_placed == []


class TestActiveStrategyTickUnchanged:
    def test_live_strategy_still_runs_entry_and_exit(self, db_session, monkeypatch):
        s = _make_strategy(db_session, status="LIVE")
        calls = []
        monkeypatch.setattr("api.custom_strategy_scheduler._try_exit", lambda *a, **k: calls.append("exit"))
        monkeypatch.setattr("api.custom_strategy_scheduler._try_entry", lambda *a, **k: calls.append("entry"))

        _tick_one_strategy(db_session, s, {"live": FakeBroker(), "paper": FakeBroker()})

        assert calls == ["exit", "entry"]
