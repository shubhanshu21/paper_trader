"""
tests/test_instrument_sync_scheduler.py — api/instrument_sync_scheduler.py,
added because the `instruments` MySQL table (routes_terminal.py's search/
autocomplete) had nothing writing to it at all — a real, permanently
stale table, found via a live user report that Terminal search didn't
match what the rest of the app (InstrumentCache) actually saw. This
task syncs InstrumentCache's CSV-backed DataFrame into that table, once
at API startup (eager — no more waiting on whichever request happens to
need the CSV cache first) and once daily thereafter.

Real automate_test MySQL schema (see tests/conftest.py) — no network
(InstrumentCache.get_or_refresh is faked directly).
"""
import pandas as pd
import pytest
from sqlalchemy.exc import IntegrityError

import automate.api.instrument_sync_scheduler as sched
from automate.db.models import Instrument


@pytest.fixture(autouse=True)
def _patch_session(db_session_factory, monkeypatch):
    monkeypatch.setattr(sched, "SessionLocal", db_session_factory)


@pytest.fixture()
def fake_df():
    return pd.DataFrame([
        {
            "instrument_key": "NSE_EQ|INE002A01018", "exchange_token": "2885", "symbol": "RELIANCE",
            "name": "RELIANCE INDUSTRIES", "last_price": 2500.5, "expiry": "", "strike": "",
            "tick_size": 0.05, "lot_size": 1, "instrument_type": "EQ", "option_type": "",
            "exchange": "NSE_EQ",
        },
        {
            "instrument_key": "NSE_FO|CE1", "exchange_token": "12345", "symbol": "RELIANCE26AUG2500CE",
            "name": "", "last_price": "", "expiry": "2026-08-27", "strike": 2500.0,
            "tick_size": 0.05, "lot_size": 500, "instrument_type": "OPTSTK", "option_type": "CE",
            "exchange": "NSE_FO",
        },
    ])


class TestSyncOnce:
    def test_writes_rows_with_empty_strings_converted_to_null(self, db_session, fake_df, monkeypatch):
        monkeypatch.setattr(sched._instrument_cache, "get_or_refresh", lambda: fake_df)

        count = sched._sync_once()

        assert count == 2
        rows = db_session.query(Instrument).order_by(Instrument.instrument_key).all()
        assert len(rows) == 2
        equity = next(r for r in rows if r.symbol == "RELIANCE")
        assert equity.expiry is None  # '' -> NULL, not the literal empty string
        assert equity.strike is None
        assert float(equity.last_price) == 2500.5

        option = next(r for r in rows if r.instrument_type == "OPTSTK")
        assert option.name is None
        assert option.last_price is None
        assert float(option.strike) == 2500.0

    def test_replaces_stale_rows_rather_than_appending(self, db_session, fake_df, monkeypatch):
        monkeypatch.setattr(sched._instrument_cache, "get_or_refresh", lambda: fake_df)
        sched._sync_once()

        # A symbol that no longer exists in the new snapshot (e.g. an expired contract) must be gone after resync.
        smaller_df = fake_df.iloc[[0]]
        monkeypatch.setattr(sched._instrument_cache, "get_or_refresh", lambda: smaller_df)
        count = sched._sync_once()

        assert count == 1
        assert db_session.query(Instrument).count() == 1

    def test_empty_snapshot_is_a_noop_and_does_not_wipe_existing_rows(self, db_session, fake_df, monkeypatch):
        monkeypatch.setattr(sched._instrument_cache, "get_or_refresh", lambda: fake_df)
        sched._sync_once()

        monkeypatch.setattr(sched._instrument_cache, "get_or_refresh", lambda: pd.DataFrame([]))
        count = sched._sync_once()

        assert count == 0
        assert db_session.query(Instrument).count() == 2  # untouched — an empty df is treated as "nothing to sync", not "wipe everything"

    def test_failure_partway_rolls_back_to_previous_data(self, db_session, fake_df, monkeypatch):
        monkeypatch.setattr(sched._instrument_cache, "get_or_refresh", lambda: fake_df)
        sched._sync_once()
        assert db_session.query(Instrument).count() == 2

        broken_df = pd.DataFrame([{"instrument_key": None, "exchange_token": None, "symbol": None,
                                    "name": None, "last_price": None, "expiry": None, "strike": None,
                                    "tick_size": None, "lot_size": None, "instrument_type": None,
                                    "option_type": None, "exchange": None}])
        monkeypatch.setattr(sched._instrument_cache, "get_or_refresh", lambda: broken_df)
        with pytest.raises(IntegrityError):
            sched._sync_once()

        # A NOT NULL violation on instrument_key must roll back the DELETE
        # too — the table must still have YESTERDAY's valid data, not be left empty.
        assert db_session.query(Instrument).count() == 2


class TestClean:
    def test_empty_string_becomes_none(self):
        assert sched._clean("") is None

    def test_none_stays_none(self):
        assert sched._clean(None) is None

    def test_real_value_passes_through(self):
        assert sched._clean(2500.5) == 2500.5
        assert sched._clean(0) == 0  # falsy but real — must not be treated as missing
