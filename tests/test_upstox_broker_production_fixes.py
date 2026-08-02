"""
tests/test_upstox_broker_production_fixes.py — regression tests for three
critical bugs found in a production-grade audit of the live broker path:

1. UpstoxBroker.resolve_instrument_key() only tried the NSE equity segment
   (resolve_equity_key), never falling back to the INDEX segment like
   InstrumentCache.resolve_key() does — meaning every live/paper custom
   strategy on NIFTY/BANKNIFTY/FINNIFTY/MIDCPNIFTY would raise
   RuntimeError the instant RuleBasedStrategy.__init__() tried to resolve
   its instrument_key. Backtests were unaffected (MockBroker already used
   resolve_key() directly).

2. _to_upstox_product(): the rest of the codebase (BaseBroker's own
   default, MockBroker, PaperBroker, backtest engines, config.py) uses
   the human-readable 'NRML'/'MIS'/'CNC' product convention, but Upstox's
   real SDK models (PlaceOrderV3Request, Instrument) validate `product`
   against a strict ['I', 'D', 'MTF'] enum and raise ValueError on
   anything else — meaning EVERY live order from the custom-strategy
   builder (which never overrides RuleBasedStrategy's "NRML" default)
   would crash before even reaching the network. Paper trading and
   backtests were unaffected (neither validates the string).

No network access — UpstoxBroker is constructed with dry_run + a fake
token (no network call happens in __init__), and its instrument cache /
SDK API objects are monkeypatched directly.
"""
from unittest.mock import MagicMock, patch

import pytest

from automate.broker.upstox_broker import UpstoxBroker, _to_upstox_product


@pytest.fixture()
def broker():
    return UpstoxBroker(access_token="fake-token-for-tests", dry_run=True)


class TestProductTranslation:
    def test_nrml_maps_to_d(self):
        assert _to_upstox_product("NRML") == "D"

    def test_mis_maps_to_i(self):
        assert _to_upstox_product("MIS") == "I"

    def test_cnc_maps_to_d(self):
        assert _to_upstox_product("CNC") == "D"

    def test_already_valid_codes_pass_through(self):
        assert _to_upstox_product("D") == "D"
        assert _to_upstox_product("I") == "I"
        assert _to_upstox_product("MTF") == "MTF"

    def test_case_insensitive(self):
        assert _to_upstox_product("nrml") == "D"

    def test_unknown_product_raises_rather_than_silently_passing_through(self):
        with pytest.raises(ValueError):
            _to_upstox_product("SOMETHING_ELSE")


class TestResolveInstrumentKeyIndexFallback:
    def test_falls_back_to_index_segment_for_an_index_symbol(self, broker):
        """The exact bug: NIFTY has no NSE_EQ row, only resolve_key()'s index fallback finds it."""
        broker._cache = MagicMock()
        broker._cache.resolve_key.return_value = "NSE_INDEX|Nifty 50"
        assert broker.resolve_instrument_key("NIFTY") == "NSE_INDEX|Nifty 50"
        broker._cache.resolve_key.assert_called_once_with("NIFTY")

    def test_still_resolves_a_plain_stock(self, broker):
        broker._cache = MagicMock()
        broker._cache.resolve_key.return_value = "NSE_EQ|INE002A01018"
        assert broker.resolve_instrument_key("RELIANCE") == "NSE_EQ|INE002A01018"

    def test_raises_when_truly_unresolvable(self, broker):
        broker._cache = MagicMock()
        broker._cache.resolve_key.return_value = None
        with pytest.raises(RuntimeError):
            broker.resolve_instrument_key("NOT_A_REAL_SYMBOL")


class TestPlaceOrderUsesTranslatedProduct:
    def test_nrml_product_no_longer_crashes_place_order(self, broker):
        """
        The exact bug: RuleBasedStrategy's default product="NRML" used to
        be forwarded straight into PlaceOrderV3Request, which raises
        ValueError since the SDK only accepts ['I', 'D', 'MTF']. dry_run
        still logs (not places) the order but exercises the exact same
        PlaceOrderV3Request construction path — this must not raise.
        """
        order_id = broker.place_sell_order(
            instrument_token="NSE_FO|12345", quantity=50, product="NRML", order_type="MARKET", tag="test",
        )
        assert order_id is None  # dry_run: no real order placed, but no crash either

    def test_mis_product_no_longer_crashes_place_order(self, broker):
        order_id = broker.place_buy_order(
            instrument_token="NSE_FO|12345", quantity=50, product="MIS", order_type="MARKET", tag="test",
        )
        assert order_id is None


class TestGetRequiredMargin:
    def test_translates_product_before_calling_margin_api(self, broker):
        fake_response = MagicMock()
        fake_response.status = "success"
        fake_response.data.required_margin = 12345.67
        broker._charge_api = MagicMock()
        broker._charge_api.post_margin.return_value = fake_response

        result = broker.get_required_margin("NSE_FO|12345", 50, "SELL", product="NRML")

        assert result == 12345.67
        call_kwargs = broker._charge_api.post_margin.call_args.kwargs
        instrument = call_kwargs["body"].instruments[0]
        assert instrument.product == "D"  # translated, not the raw "NRML"

    def test_returns_none_on_api_failure_rather_than_raising(self, broker):
        broker._charge_api = MagicMock()
        broker._charge_api.post_margin.side_effect = RuntimeError("network error")
        assert broker.get_required_margin("NSE_FO|12345", 50, "SELL") is None

    def test_returns_none_on_non_success_status(self, broker):
        fake_response = MagicMock()
        fake_response.status = "error"
        broker._charge_api = MagicMock()
        broker._charge_api.post_margin.return_value = fake_response
        assert broker.get_required_margin("NSE_FO|12345", 50, "SELL") is None


class TestGetBrokerPositions:
    """
    get_broker_positions() — real broker-side net open quantity per
    instrument, added for utils/position_reconciliation.py's crash-window
    safety net (see that module's docstring for the scenario this guards
    against).
    """
    def test_returns_net_quantity_keyed_by_instrument_token(self, broker):
        long_pos = MagicMock(instrument_token="NSE_FO|1", quantity=50)
        short_pos = MagicMock(instrument_token="NSE_FO|2", quantity=-25)
        flat_pos = MagicMock(instrument_token="NSE_FO|3", quantity=0)  # Upstox omits these in practice, but tolerate it
        broker._portfolio_api = MagicMock()
        broker._portfolio_api.get_positions.return_value = MagicMock(data=[long_pos, short_pos, flat_pos])

        result = broker.get_broker_positions()

        assert result == {"NSE_FO|1": 50, "NSE_FO|2": -25}
        broker._portfolio_api.get_positions.assert_called_once_with(api_version="2.0")

    def test_returns_empty_dict_when_no_positions(self, broker):
        broker._portfolio_api = MagicMock()
        broker._portfolio_api.get_positions.return_value = MagicMock(data=[])
        assert broker.get_broker_positions() == {}

    def test_returns_none_when_response_data_is_none(self, broker):
        broker._portfolio_api = MagicMock()
        broker._portfolio_api.get_positions.return_value = MagicMock(data=None)
        assert broker.get_broker_positions() is None

    def test_returns_none_on_api_failure_rather_than_raising(self, broker):
        broker._portfolio_api = MagicMock()
        broker._portfolio_api.get_positions.side_effect = RuntimeError("network error")
        assert broker.get_broker_positions() is None
