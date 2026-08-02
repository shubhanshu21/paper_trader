"""
tests/test_market_status_endpoint.py — GET /api/market-status, added so
the frontend header can show whether NSE F&O is open right now instead
of leaving "will my strategy actually trade today?" a mystery. Reuses
the exact same assert_market_is_open() check every scheduler already
gates entries/exits on — no separate logic to drift out of sync.
"""
import automate.api.main as main_module


class TestMarketStatus:
    def test_returns_closed_with_a_reason_outside_market_hours(self, monkeypatch):
        def _raise():
            raise RuntimeError("[SEBI] Market CLOSED — outside trading hours.")
        monkeypatch.setattr("automate.compliance.sebi_rules.assert_market_is_open", _raise)

        result = main_module.market_status()

        assert result["open"] is False
        assert "CLOSED" in result["message"]
        assert "server_time_ist" in result

    def test_returns_open_when_market_is_open(self, monkeypatch):
        monkeypatch.setattr("automate.compliance.sebi_rules.assert_market_is_open", lambda: None)

        result = main_module.market_status()

        assert result["open"] is True
        assert result["message"] == "Market is open"

    def test_never_raises_even_if_the_calendar_check_itself_errors(self, monkeypatch):
        """assert_market_is_open raises RuntimeError for 'closed', not any other exception type — but this endpoint must never 500 the header badge either way."""
        monkeypatch.setattr("automate.compliance.sebi_rules.assert_market_is_open", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        result = main_module.market_status()
        assert result["open"] is False
