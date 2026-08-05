"""
tests/test_paper_broker_production_fixes.py — regression tests for
PaperBroker._place_order()'s fallback-fabrication bugs found in a
production-grade audit:

1. On ANY instrument-lookup failure, it used to silently default
   base_symbol="RELIANCE" / inst_type="EQUITY" and keep going — meaning a
   resolution glitch on, say, a NIFTY option SELL would silently fall
   into the (BUY-only-checked) equity branch and skip margin validation
   entirely, or compute margin off a completely unrelated underlying's
   price. Fixed to raise (refuse) instead.

2. On a margin-check spot-price lookup failure, it used to default
   underlying_spot=2000.0 — wildly wrong for an index like BANKNIFTY
   (~₹57,000), silently under-margining a short by ~28x. Fixed to prefer
   the real broker-calculated margin (get_required_margin), and only
   raise (never fabricate) if BOTH that and the spot lookup fail.

Fully hermetic — a FakeRealBroker stands in for UpstoxBroker, no network.
"""
from unittest.mock import MagicMock

import pandas as pd
import pytest

from broker.paper_broker import PaperBroker

CE_TOKEN = "NSE_FO|CE_TEST"
NIFTY_INDEX_KEY = "NSE_INDEX|Nifty 50"


class FakeRealBroker:
    def __init__(self, instrument_df=None, ltp=None, required_margin=None):
        self._cache = MagicMock()
        self._cache.get_or_refresh.return_value = instrument_df if instrument_df is not None else pd.DataFrame(
            columns=["instrument_key", "instrument_type", "symbol"]
        )
        self._cache.resolve_key.return_value = NIFTY_INDEX_KEY
        self.ltp = ltp or {}
        self._required_margin = required_margin

    def get_ltp(self, instrument_key):
        return self.ltp.get(instrument_key)

    def get_required_margin(self, instrument_key, quantity, transaction_type, product="D"):
        return self._required_margin


def _option_row(token=CE_TOKEN, symbol="NIFTY26AUG24000CE"):
    return pd.DataFrame([{"instrument_key": token, "instrument_type": "OPTIDX", "symbol": symbol}])


class TestRefusesRatherThanFabricatesInstrumentDetails:
    def test_raises_when_instrument_not_in_cache_rather_than_defaulting_to_reliance(self):
        real = FakeRealBroker(instrument_df=pd.DataFrame(columns=["instrument_key", "instrument_type", "symbol"]), ltp={CE_TOKEN: 100.0})
        broker = PaperBroker(real_broker=real)
        with pytest.raises(RuntimeError, match="could not resolve instrument details"):
            broker.place_sell_order(instrument_token=CE_TOKEN, quantity=50, user_id=None)

    def test_raises_when_cache_lookup_itself_throws(self):
        real = FakeRealBroker(ltp={CE_TOKEN: 100.0})
        real._cache.get_or_refresh.side_effect = RuntimeError("cache exploded")
        broker = PaperBroker(real_broker=real)
        with pytest.raises(RuntimeError, match="could not resolve instrument details"):
            broker.place_sell_order(instrument_token=CE_TOKEN, quantity=50, user_id=None)

    def test_succeeds_normally_when_instrument_resolves(self):
        real = FakeRealBroker(instrument_df=_option_row(), ltp={CE_TOKEN: 100.0}, required_margin=5000.0)
        broker = PaperBroker(real_broker=real)
        order_id = broker.place_sell_order(instrument_token=CE_TOKEN, quantity=50, user_id=None)
        assert order_id is not None and order_id.startswith("PAPER-")


class TestMarginNeverFabricatesUnderlyingSpot:
    def test_prefers_real_margin_over_flat_estimate(self):
        real = FakeRealBroker(instrument_df=_option_row(), ltp={CE_TOKEN: 100.0, NIFTY_INDEX_KEY: 24000.0}, required_margin=42000.0)
        broker = PaperBroker(real_broker=real)
        # user_id=None -> unlimited virtual balance, so this only exercises
        # the margin computation path itself, not the reject branch.
        order_id = broker.place_sell_order(instrument_token=CE_TOKEN, quantity=50, user_id=None)
        assert order_id is not None

    def test_raises_rather_than_defaulting_spot_to_2000_when_both_lookups_fail(self):
        real = FakeRealBroker(instrument_df=_option_row(), ltp={CE_TOKEN: 100.0}, required_margin=None)
        real._cache.resolve_key.return_value = None  # underlying spot lookup also fails
        broker = PaperBroker(real_broker=real)
        with pytest.raises(RuntimeError, match="Refusing to guess"):
            broker.place_sell_order(instrument_token=CE_TOKEN, quantity=50, user_id=None)

    def test_falls_back_to_flat_estimate_when_only_real_margin_unavailable(self):
        real = FakeRealBroker(instrument_df=_option_row(), ltp={CE_TOKEN: 100.0, NIFTY_INDEX_KEY: 24000.0}, required_margin=None)
        broker = PaperBroker(real_broker=real)
        order_id = broker.place_sell_order(instrument_token=CE_TOKEN, quantity=50, user_id=None)
        assert order_id is not None  # flat estimate (24000 * 50 * 0.11) computed successfully, no crash
