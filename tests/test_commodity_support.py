"""
tests/test_commodity_support.py — MCX commodity support added to the
strategy builder (live/paper only — backtesting stays blocked, no
historical MCX dataset exists). Covers InstrumentCache's new MCX
resolution paths (resolve_key/resolve_commodity_key/resolve_lot_size/
resolve_strike_step/list_tradable_symbols all previously hardcoded to
NSE_FO/NSE_EQ/NSE_INDEX only) and margin.py's is_commodity_instrument_key.

Hermetic: InstrumentCache is built via __new__() (bypassing __init__) with
get_or_refresh() monkeypatched to return a small synthetic instrument
master shaped like the real one — no network/real cache file needed.
"""
import pandas as pd
import pytest

from automate.utils.instrument_cache import InstrumentCache
from automate.utils.margin import is_commodity_instrument_key


def _synthetic_df() -> pd.DataFrame:
    rows = [
        # NSE equity/index (sanity — must stay unaffected)
        {"instrument_key": "NSE_EQ|INE002A01018", "symbol": "RELIANCE", "name": "RELIANCE INDUSTRIES LTD",
         "exchange": "NSE_EQ", "instrument_type": "EQ", "expiry": "", "strike": "", "option_type": "", "lot_size": 1},
        {"instrument_key": "NSE_INDEX|Nifty 50", "symbol": "NIFTY", "name": "Nifty 50",
         "exchange": "NSE_INDEX", "instrument_type": "INDEX", "expiry": "", "strike": "", "option_type": "", "lot_size": 1},
        {"instrument_key": "NSE_FO|1", "symbol": "RELIANCE26AUGFUT", "name": "",
         "exchange": "NSE_FO", "instrument_type": "FUTSTK", "expiry": "2026-08-27", "strike": "", "option_type": "", "lot_size": 500},
        {"instrument_key": "NSE_FO|2", "symbol": "RELIANCE26AUG1500CE", "name": "",
         "exchange": "NSE_FO", "instrument_type": "OPTSTK", "expiry": "2026-08-27", "strike": 1500.0, "option_type": "CE", "lot_size": 500},
        {"instrument_key": "NSE_FO|3", "symbol": "RELIANCE26AUG1520CE", "name": "",
         "exchange": "NSE_FO", "instrument_type": "OPTSTK", "expiry": "2026-08-27", "strike": 1520.0, "option_type": "CE", "lot_size": 500},
        # MCX commodity — GOLD future + two option strikes
        {"instrument_key": "MCX_FO|100", "symbol": "GOLD27FEBFUT", "name": "",
         "exchange": "MCX_FO", "instrument_type": "FUTCOM", "expiry": "2027-02-05", "strike": "", "option_type": "", "lot_size": 1},
        {"instrument_key": "MCX_FO|101", "symbol": "GOLD27FEB124000CE", "name": "",
         "exchange": "MCX_FO", "instrument_type": "OPTFUT", "expiry": "2027-02-26", "strike": 124000.0, "option_type": "CE", "lot_size": 1},
        {"instrument_key": "MCX_FO|102", "symbol": "GOLD27FEB124500CE", "name": "",
         "exchange": "MCX_FO", "instrument_type": "OPTFUT", "expiry": "2027-02-26", "strike": 124500.0, "option_type": "CE", "lot_size": 1},
    ]
    return pd.DataFrame(rows)


@pytest.fixture()
def cache(monkeypatch):
    ic = InstrumentCache.__new__(InstrumentCache)
    monkeypatch.setattr(ic, "get_or_refresh", lambda force=False: _synthetic_df())
    return ic


class TestResolveKeyFallsBackToCommodity:
    def test_resolves_a_commodity_via_nearest_future(self, cache):
        assert cache.resolve_key("GOLD") == "MCX_FO|100"

    def test_still_resolves_a_stock(self, cache):
        assert cache.resolve_key("RELIANCE") == "NSE_EQ|INE002A01018"

    def test_still_resolves_an_index(self, cache):
        assert cache.resolve_key("NIFTY") == "NSE_INDEX|Nifty 50"

    def test_unknown_symbol_resolves_to_none(self, cache):
        assert cache.resolve_key("NOT_A_REAL_SYMBOL") is None


class TestResolveCommodityKey:
    def test_returns_key_and_expiry(self, cache):
        key, expiry = cache.resolve_commodity_key("GOLD")
        assert key == "MCX_FO|100"
        assert expiry == "2027-02-05"

    def test_none_for_a_non_commodity_symbol(self, cache):
        assert cache.resolve_commodity_key("RELIANCE") is None


class TestLotSizeAndStrikeStepCoverMcx:
    def test_resolve_lot_size_finds_mcx_contracts(self, cache):
        assert cache.resolve_lot_size("GOLD") == 1

    def test_resolve_lot_size_still_finds_nse_contracts(self, cache):
        assert cache.resolve_lot_size("RELIANCE") == 500

    def test_resolve_strike_step_finds_mcx_contracts(self, cache):
        assert cache.resolve_strike_step("GOLD") == 500.0  # 124500 - 124000

    def test_resolve_strike_step_still_finds_nse_contracts(self, cache):
        assert cache.resolve_strike_step("RELIANCE") == 20.0  # 1520 - 1500


class TestListTradableSymbolsIncludesCommodities:
    def test_commodities_key_present_and_populated(self, cache):
        result = cache.list_tradable_symbols()
        assert "commodities" in result
        assert result["commodities"] == ["GOLD"]
        assert result["stocks"] == ["RELIANCE"]


class TestIsCommodityInstrumentKey:
    def test_mcx_key_is_commodity(self):
        assert is_commodity_instrument_key("MCX_FO|100") is True

    def test_nse_key_is_not_commodity(self):
        assert is_commodity_instrument_key("NSE_FO|1") is False
        assert is_commodity_instrument_key("NSE_EQ|INE002A01018") is False

    def test_none_is_not_commodity(self):
        assert is_commodity_instrument_key(None) is False
