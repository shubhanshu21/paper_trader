"""
tests/test_performance_real_data.py — api/routes_performance.py's
_closed_trades/_filter_period, the core logic behind the Trade Journal's
metrics. Exercises _closed_trades directly against a real (test) DB
session rather than going through the FastAPI route handlers, which each
open their own SessionLocal() — same seam test_scheduler_reconciliation_
wiring.py uses for _reconcile_live_positions.

The one thing worth re-proving here (beyond "the numbers come out
right"): a multi-leg basket (e.g. a 3-leg spread closing together)
collapses into ONE trade for win-rate/profit-factor purposes, not one
per leg — the whole reason this module reuses routes_leaderboard.py's
basket-grouping instead of just iterating CustomStrategyPosition rows.
"""
import json
from datetime import datetime, timedelta

import pytest

from api.routes_performance import _closed_trades, _filter_period
from db.models import CustomStrategy, CustomStrategyPosition


@pytest.fixture()
def db(db_session):
    return db_session


def _make_strategy(db, **overrides):
    defaults = {
        "user_id": 1, "name": "Test Strategy", "instrument_type": "INDEX", "strategy_type": "CUSTOM",
        "option_type": "BOTH", "symbols": json.dumps(["NIFTY"]), "status": "PAPER_TRADING",
    }
    defaults.update(overrides)
    s = CustomStrategy(**defaults)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _make_leg(strategy_id, instrument_key, *, entry_price, exit_price, quantity=50,
              transaction_type="SELL", mode="paper", opened_at=None, closed_at=None):
    return CustomStrategyPosition(
        strategy_id=strategy_id, leg_index=0, mode=mode, instrument_key=instrument_key,
        instrument_type="OPTION", option_type="CE", strike=24000, expiry="2026-08-27",
        transaction_type=transaction_type, quantity=quantity, entry_price=entry_price,
        exit_price=exit_price, status="CLOSED",
        opened_at=opened_at or datetime.now(), closed_at=closed_at or datetime.now(),
    )


class TestClosedTrades:
    def test_a_single_leg_closed_position_becomes_one_trade(self, db, monkeypatch):
        monkeypatch.setattr(
            "api.routes_performance._is_leg_for_symbol", lambda instrument_key, symbol: symbol == "NIFTY",
        )
        strategy = _make_strategy(db)
        db.add(_make_leg(strategy.id, "NSE_FO|1", entry_price=100.0, exit_price=80.0, quantity=50))
        db.commit()

        trades = _closed_trades(db, user_id=1)
        assert len(trades) == 1
        assert trades[0]["strategy"] == "Test Strategy"
        assert trades[0]["symbol"] == "NIFTY"
        assert trades[0]["legs"] == 1
        # SELL, entry 100 -> exit 80, qty 50: (100-80)*50 = 1000 gross profit, net after charges < gross.
        assert trades[0]["pnl"] > 0

    def test_multi_leg_basket_opened_together_collapses_into_one_trade(self, db, monkeypatch):
        # The real reason this module reuses the leaderboard's basket
        # grouping instead of iterating raw legs: a 3-leg spread closing
        # is ONE trade for win-rate/profit-factor purposes, not three.
        monkeypatch.setattr(
            "api.routes_performance._is_leg_for_symbol", lambda instrument_key, symbol: symbol == "NIFTY",
        )
        strategy = _make_strategy(db)
        same_moment = datetime(2026, 8, 6, 10, 0, 0)
        for i in range(3):
            db.add(_make_leg(
                strategy.id, f"NSE_FO|{i}", entry_price=50.0, exit_price=40.0,
                opened_at=same_moment, closed_at=same_moment + timedelta(minutes=5),
            ))
        db.commit()

        trades = _closed_trades(db, user_id=1)
        assert len(trades) == 1
        assert trades[0]["legs"] == 3

    def test_legs_opened_far_apart_stay_separate_trades(self, db, monkeypatch):
        monkeypatch.setattr(
            "api.routes_performance._is_leg_for_symbol", lambda instrument_key, symbol: symbol == "NIFTY",
        )
        strategy = _make_strategy(db)
        db.add(_make_leg(strategy.id, "NSE_FO|1", entry_price=100.0, exit_price=90.0,
                          opened_at=datetime(2026, 8, 6, 9, 0, 0)))
        db.add(_make_leg(strategy.id, "NSE_FO|2", entry_price=100.0, exit_price=90.0,
                          opened_at=datetime(2026, 8, 6, 14, 0, 0)))
        db.commit()

        trades = _closed_trades(db, user_id=1)
        assert len(trades) == 2

    def test_a_leg_with_no_exit_price_is_skipped_not_crashed(self, db, monkeypatch):
        monkeypatch.setattr(
            "api.routes_performance._is_leg_for_symbol", lambda instrument_key, symbol: symbol == "NIFTY",
        )
        strategy = _make_strategy(db)
        leg = _make_leg(strategy.id, "NSE_FO|1", entry_price=100.0, exit_price=None)
        db.add(leg)
        db.commit()

        trades = _closed_trades(db, user_id=1)
        assert trades == []

    def test_a_user_with_no_strategies_gets_no_trades(self, db):
        assert _closed_trades(db, user_id=999) == []

    def test_mode_filter_restricts_to_paper_or_live(self, db, monkeypatch):
        monkeypatch.setattr(
            "api.routes_performance._is_leg_for_symbol", lambda instrument_key, symbol: symbol == "NIFTY",
        )
        strategy = _make_strategy(db)
        db.add(_make_leg(strategy.id, "NSE_FO|1", entry_price=100.0, exit_price=90.0, mode="paper",
                          opened_at=datetime(2026, 8, 6, 9, 0, 0)))
        db.add(_make_leg(strategy.id, "NSE_FO|2", entry_price=100.0, exit_price=90.0, mode="live",
                          opened_at=datetime(2026, 8, 6, 14, 0, 0)))
        db.commit()

        assert len(_closed_trades(db, user_id=1, mode="paper")) == 1
        assert len(_closed_trades(db, user_id=1, mode="live")) == 1
        assert len(_closed_trades(db, user_id=1)) == 2


class TestFilterPeriod:
    def _trade(self, exit_date):
        return {"exit_date": exit_date, "pnl": 1.0}

    def test_all_returns_everything(self):
        trades = [self._trade("2020-01-01T00:00:00")]
        assert _filter_period(trades, "all") == trades

    def test_today_matches_only_todays_exit_date(self):
        today = self._trade(datetime.now().isoformat())
        old = self._trade((datetime.now() - timedelta(days=5)).isoformat())
        result = _filter_period([today, old], "today")
        assert result == [today]

    def test_week_excludes_trades_older_than_seven_days(self):
        recent = self._trade((datetime.now() - timedelta(days=2)).isoformat())
        old = self._trade((datetime.now() - timedelta(days=10)).isoformat())
        result = _filter_period([recent, old], "week")
        assert result == [recent]

    def test_unknown_period_falls_back_to_returning_everything(self):
        trades = [self._trade("2020-01-01T00:00:00")]
        assert _filter_period(trades, "bogus") == trades
