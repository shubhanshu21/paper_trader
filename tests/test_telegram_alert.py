"""
tests/test_telegram_alert.py — utils/telegram_alert.py never raises and
never calls the network when unconfigured, and posts correctly when it is.
No real Telegram credentials or network access needed (requests.post mocked).
"""
import automate.utils.telegram_alert as telegram_alert
from automate.config import TelegramConfig


def test_noop_when_unconfigured(monkeypatch):
    monkeypatch.setattr(TelegramConfig, "BOT_TOKEN", "")
    monkeypatch.setattr(TelegramConfig, "CHAT_ID", "")

    calls = []
    monkeypatch.setattr(telegram_alert.requests, "post", lambda *a, **k: calls.append((a, k)))

    telegram_alert.send_telegram_alert("should not be sent")
    assert calls == []


def test_sends_when_configured(monkeypatch):
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
    assert payload == {"chat_id": "12345", "text": "hello"}


def test_never_raises_on_network_error(monkeypatch):
    monkeypatch.setattr(TelegramConfig, "BOT_TOKEN", "fake-token")
    monkeypatch.setattr(TelegramConfig, "CHAT_ID", "12345")

    import requests

    def raise_conn_error(*a, **k):
        raise requests.RequestException("boom")

    monkeypatch.setattr(telegram_alert.requests, "post", raise_conn_error)

    telegram_alert.send_telegram_alert("hello")  # must not raise


def test_alert_helpers_format_and_send(monkeypatch):
    monkeypatch.setattr(TelegramConfig, "BOT_TOKEN", "fake-token")
    monkeypatch.setattr(TelegramConfig, "CHAT_ID", "12345")

    sent = []
    monkeypatch.setattr(telegram_alert, "send_telegram_alert", lambda msg: sent.append(msg))

    telegram_alert.alert_error("TestStrategy", "something broke")
    telegram_alert.alert_trade_opened("ten_percent_otm_strangle", "paper", "RELIANCE", "CE 2900 PE 2300")
    telegram_alert.alert_trade_closed("ten_percent_otm_strangle", "live", "TCS", "reason=EXPIRY")
    telegram_alert.alert_manual_intervention("position #3 stuck")

    assert len(sent) == 4
    assert "TestStrategy" in sent[0]
    assert "RELIANCE" in sent[1] and "paper" in sent[1]
    assert "TCS" in sent[2] and "live" in sent[2]
    assert "position #3 stuck" in sent[3]
