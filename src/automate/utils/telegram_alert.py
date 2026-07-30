"""
utils/telegram_alert.py — Fire-and-forget operational alerts to Telegram.

Covers the things a headless daemon can silently fail at without anyone
noticing: an expired/invalid broker token, a trade actually being taken,
a trade failing to enter, a position failing to exit cleanly, and — via
the daily heartbeat — the daemon itself hanging or crashing on a quiet
day with no trades/errors, which would otherwise look identical to
"everything's fine" from the outside. Callers just describe WHAT
happened; this module never raises — a Telegram outage must never be
able to break the trading flow that's reporting to it.
"""
import logging

import requests

from automate.config import TelegramConfig

log = logging.getLogger(__name__)

_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
_TIMEOUT_SEC = 5

_warned_unconfigured = False


def send_telegram_alert(message: str) -> None:
    """Best-effort send. Never raises — logs and returns on any failure."""
    global _warned_unconfigured
    if not TelegramConfig.is_configured():
        if not _warned_unconfigured:
            log.warning(
                "Telegram alerts not configured (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID) — "
                "skipping alerts for this run. Set both in .env to enable."
            )
            _warned_unconfigured = True
        return

    try:
        resp = requests.post(
            _API_URL.format(token=TelegramConfig.BOT_TOKEN),
            json={"chat_id": TelegramConfig.CHAT_ID, "text": message},
            timeout=_TIMEOUT_SEC,
        )
        if resp.status_code != 200:
            log.error("Telegram alert failed: HTTP %s — %s", resp.status_code, resp.text)
    except requests.RequestException as exc:
        log.error("Telegram alert failed (network/timeout): %s", exc)


def alert_error(source: str, message: str) -> None:
    send_telegram_alert(f"🔴 ERROR | {source}\n{message}")


def alert_trade_opened(strategy_name: str, mode: str, symbol: str, details: str) -> None:
    icon = "🧪" if mode == "paper" else "✅"
    send_telegram_alert(f"{icon} TRADE OPENED | {strategy_name}/{symbol} (mode={mode})\n{details}")


def alert_trade_closed(strategy_name: str, mode: str, symbol: str, details: str) -> None:
    icon = "🧪" if mode == "paper" else "✅"
    send_telegram_alert(f"{icon} TRADE CLOSED | {strategy_name}/{symbol} (mode={mode})\n{details}")


def alert_manual_intervention(message: str) -> None:
    send_telegram_alert(f"🚨 MANUAL INTERVENTION REQUIRED\n{message}")


def alert_heartbeat(modes: dict, open_position_count: int, market_open: bool) -> None:
    """
    Once-a-day "still alive" ping — see run_daemon.py's heartbeat marker.
    Trade/error alerts only fire on activity, so a hung or crashed daemon
    on an otherwise-quiet day (no trades, no errors) would look identical
    to "nothing happened, all fine" from the outside. This is the signal
    that the process itself is actually up and ticking.
    """
    strategies_line = ", ".join(f"{name} ({mode})" for name, mode in modes.items()) or "none configured"
    send_telegram_alert(
        f"💓 Daemon heartbeat — still running.\n"
        f"Strategies: {strategies_line}\n"
        f"Open positions: {open_position_count}\n"
        f"Market open right now: {'yes' if market_open else 'no'}"
    )
