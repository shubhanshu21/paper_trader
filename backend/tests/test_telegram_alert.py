"""
tests/test_telegram_alert.py — utils/telegram_alert.py never raises and
never calls the network when unconfigured, posts correctly when it is,
and formats messages as structured HTML (bold titles, monospace detail
blocks, PnL icons) rather than flat single-line text — with every
user-controlled value HTML-escaped first, since parse_mode="HTML" will
otherwise choke on stray '<'/'&' in a strategy name or error message.
No real Telegram credentials or network access needed (requests.post mocked).
"""
import utils.telegram_alert as telegram_alert
from config import TelegramConfig


def test_noop_when_unconfigured(monkeypatch):
    monkeypatch.setattr(TelegramConfig, "BOT_TOKEN", "")
    monkeypatch.setattr(TelegramConfig, "CHAT_ID", "")

    calls = []
    monkeypatch.setattr(telegram_alert.requests, "post", lambda *a, **k: calls.append((a, k)))

    telegram_alert.send_telegram_alert("should not be sent")
    assert calls == []


def test_sends_when_configured_with_html_parse_mode(monkeypatch):
    monkeypatch.setattr(TelegramConfig, "BOT_TOKEN", "fake-token")
    monkeypatch.setattr(TelegramConfig, "CHAT_ID", "12345")

    calls = []

    class _Resp:
        status_code = 200
        text = "ok"

    def fake_post(url, json, timeout):
        calls.append((url, json, timeout))
        return _Resp()

    monkeypatch.setattr(telegram_alert.requests, "post", fake_post)

    telegram_alert.send_telegram_alert("hello")
    assert len(calls) == 1
    url, payload, _ = calls[0]
    assert "fake-token" in url
    assert payload == {
        "chat_id": "12345", "text": "hello", "parse_mode": "HTML", "disable_web_page_preview": True,
    }


def test_never_raises_on_network_error(monkeypatch):
    monkeypatch.setattr(TelegramConfig, "BOT_TOKEN", "fake-token")
    monkeypatch.setattr(TelegramConfig, "CHAT_ID", "12345")

    import requests

    def raise_conn_error(*a, **k):
        raise requests.RequestException("boom")

    monkeypatch.setattr(telegram_alert.requests, "post", raise_conn_error)

    telegram_alert.send_telegram_alert("hello")  # must not raise


def test_never_raises_on_non_200(monkeypatch):
    monkeypatch.setattr(TelegramConfig, "BOT_TOKEN", "fake-token")
    monkeypatch.setattr(TelegramConfig, "CHAT_ID", "12345")

    class _Resp:
        status_code = 400
        text = "Bad Request: can't parse entities"

    monkeypatch.setattr(telegram_alert.requests, "post", lambda *a, **k: _Resp())
    telegram_alert.send_telegram_alert("hello")  # must not raise


class TestEsc:
    def test_escapes_angle_brackets_and_ampersand(self):
        assert telegram_alert.esc("<script>a & b</script>") == "&lt;script&gt;a &amp; b&lt;/script&gt;"


class TestFormatBody:
    def test_leg_fields_become_an_aligned_grid_with_action_icons(self):
        body = telegram_alert._format_body("SELL CE 24600.00@127.60 | BUY CE 25000.00@17.65")
        assert "🔴 SELL CE  24600.00 @ ₹127.60" in body
        assert "🟢 BUY  CE  25000.00 @ ₹17.65" in body

    def test_recognized_key_value_fields_become_labeled_rows_below_the_grid(self):
        body = telegram_alert._format_body("SELL CE 24600.00@127.60 | reason=STOP_LOSS | pnl=-2.51%")
        assert "🔴 SELL CE  24600.00 @ ₹127.60" in body
        assert "Reason  STOP_LOSS" in body
        # "P&L" is HTML-escaped like everything else going into an HTML
        # parse_mode message, and the "%" is NOT duplicated — every real
        # caller's "pnl=" value already carries its own trailing "%".
        assert "P&amp;L     -2.51%" in body
        assert "%%" not in body

    def test_qty_field_becomes_a_labeled_row(self):
        body = telegram_alert._format_body("SELL CE 24600.00@127.60 | qty=65")
        assert "Qty  65" in body

    def test_unrecognized_fields_are_kept_as_plain_lines_not_dropped(self):
        # "CE 2900@120.50" has no BUY/SELL prefix and no "=" — doesn't match
        # the leg grid or the key=value row, but must still show up somewhere
        # rather than silently vanish.
        body = telegram_alert._format_body("CE 2900@120.50 | expiry=2026-01-29")
        assert "CE 2900@120.50" in body
        assert "Expiry  2026-01-29" in body

    def test_html_special_characters_in_the_body_are_escaped(self):
        body = telegram_alert._format_body("error: x < y & z > 0")
        assert "&lt;" in body and "&amp;" in body and "&gt;" in body
        assert '<pre><code class="language-text">' in body and "</code></pre>" in body

    def test_declares_an_explicit_language_so_the_telegram_client_does_not_auto_guess_one(self):
        # A bare <pre> with no language gets run through the Telegram
        # CLIENT's own auto-detect-and-highlight heuristic, which was
        # mislabeling plain field dumps like "SELL CE 24600.00@127.60" as
        # "Sql". Declaring language-text stops the client from overriding
        # it with a guess.
        body = telegram_alert._format_body("SELL CE 24600.00@127.60 | BUY CE 25000.00@17.65 | pnl=-2.51%")
        assert 'class="language-text"' in body


class TestPnlIcon:
    def test_positive_pnl_gets_up_arrow(self):
        assert telegram_alert._pnl_icon("reason=TAKE_PROFIT | pnl=+40.0% | CE exit=5.00") == "📈"

    def test_negative_pnl_gets_down_arrow(self):
        assert telegram_alert._pnl_icon("reason=STOP_LOSS | pnl=-12.5% | CE exit=15.00") == "📉"

    def test_no_pnl_field_returns_empty(self):
        assert telegram_alert._pnl_icon("reason=EXPIRY") == ""


class TestAlertHelpersFormatAndSend:
    def setup_method(self):
        self.sent = []

    def _capture(self, monkeypatch):
        monkeypatch.setattr(telegram_alert, "send_telegram_alert", lambda msg: self.sent.append(msg))

    def test_alert_error(self, monkeypatch):
        self._capture(monkeypatch)
        telegram_alert.alert_error("custom_strategy_scheduler", "broker timeout")
        msg = self.sent[0]
        assert "<b>Error</b>" in msg
        assert "custom_strategy_scheduler" in msg
        assert "broker timeout" in msg

    def test_alert_error_escapes_untrusted_source_and_message(self, monkeypatch):
        self._capture(monkeypatch)
        telegram_alert.alert_error("<evil>", "x < y & z")
        msg = self.sent[0]
        assert "<evil>" not in msg
        assert "&lt;evil&gt;" in msg
        assert "x &lt; y &amp; z" in msg

    def test_alert_trade_opened_paper(self, monkeypatch):
        self._capture(monkeypatch)
        telegram_alert.alert_trade_opened(
            "ten_percent_otm_strangle", "paper", "RELIANCE",
            "SELL CE 2900@120.50 | BUY PE 2300@95.00 | qty=50 | expiry=2026-01-29",
        )
        msg = self.sent[0]
        assert "Trade Opened" in msg and "PAPER" in msg
        assert "🧪" in msg
        assert "RELIANCE" in msg
        assert "🔴 SELL CE  2900 @ ₹120.50" in msg  # leg grid row
        assert "🟢 BUY  PE  2300 @ ₹95.00" in msg
        assert "Qty     50" in msg
        assert "Expiry  2026-01-29" in msg

    def test_alert_trade_opened_live_uses_different_icon(self, monkeypatch):
        self._capture(monkeypatch)
        telegram_alert.alert_trade_opened("s", "live", "TCS", "qty=50")
        msg = self.sent[0]
        assert "LIVE" in msg
        assert "✅" in msg
        assert "🧪" not in msg

    def test_alert_trade_closed_includes_pnl_icon(self, monkeypatch):
        self._capture(monkeypatch)
        telegram_alert.alert_trade_closed(
            "s", "live", "TCS", "reason=TAKE_PROFIT | pnl=+22.0% | CE exit=5.00 PE exit=3.00",
        )
        msg = self.sent[0]
        assert "Trade Closed" in msg
        assert "📈" in msg

    def test_alert_manual_intervention(self, monkeypatch):
        self._capture(monkeypatch)
        telegram_alert.alert_manual_intervention(
            "position_id=3 strategy=Foo symbol=NIFTY CE=FAILED PE=order123 — exit incomplete, verify manually.",
        )
        msg = self.sent[0]
        assert "MANUAL INTERVENTION REQUIRED" in msg
        assert "🚨" in msg
        assert "position_id=3\nstrategy=Foo\nsymbol=NIFTY\nCE=FAILED\nPE=order123" in msg

    def test_alert_heartbeat(self, monkeypatch):
        self._capture(monkeypatch)
        telegram_alert.alert_heartbeat({"ten_percent_otm_strangle": "paper"}, 2, True)
        msg = self.sent[0]
        assert "Daemon Heartbeat" in msg
        assert "💓" in msg
        assert "Open positions   2" in msg
        assert "Market open now  yes" in msg

    def test_alert_heartbeat_escapes_strategy_names(self, monkeypatch):
        self._capture(monkeypatch)
        telegram_alert.alert_heartbeat({"<bad>": "live"}, 0, False)
        msg = self.sent[0]
        assert "<bad>" not in msg
        assert "&lt;bad&gt;" in msg
