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

Messages are sent with parse_mode="HTML" (bold titles, monospace detail
blocks) instead of flat text — every value that could contain
user-controlled or free-form text (strategy names, symbols, error
messages) MUST go through esc() first, since raw '<'/'&' would otherwise
either break Telegram's HTML parser (message silently fails to send) or
get misrendered.
"""
import logging
import re
from html import escape

import requests

from automate.config import TelegramConfig

log = logging.getLogger(__name__)

_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
_TIMEOUT_SEC = 5

_warned_unconfigured = False


def send_telegram_alert(message: str, parse_mode: str = "HTML") -> None:
    """Best-effort send. `message` must already be safe for `parse_mode` (see esc()/_format_body() below). Never raises — logs and returns on any failure."""
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
            json={
                "chat_id": TelegramConfig.CHAT_ID,
                "text": message,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            },
            timeout=_TIMEOUT_SEC,
        )
        if resp.status_code != 200:
            log.error("Telegram alert failed: HTTP %s — %s", resp.status_code, resp.text)
    except requests.RequestException as exc:
        log.error("Telegram alert failed (network/timeout): %s", exc)


def esc(value) -> str:
    """HTML-escape any value that might contain user-controlled or free-form text before interpolating it into an HTML parse_mode message."""
    return escape(str(value))


# This codebase's alert callers build their `details`/`message` strings in
# one of two shapes: " | "-joined summary fields (e.g. "CE 24000@120.50 |
# PE 24000@110.00 | qty=50"), or space-separated "key=value" tokens (e.g.
# "position_id=1 strategy=Foo symbol=NIFTY"). Both get reflowed to one
# field per line inside a <pre> block below, rather than staying a single
# hard-to-scan run-on line — a real formatting fix, not just parsing for
# parsing's sake, since these ARE the two conventions already used
# throughout the codebase's alert call sites.
_FIELD_BOUNDARY_RE = re.compile(r"\s(?=\w+=)")
_PNL_RE = re.compile(r"pnl=([+-]?\d+(?:\.\d+)?)%")


def _format_body(details: str) -> str:
    body = details.replace(" | ", "\n")
    body = _FIELD_BOUNDARY_RE.sub("\n", body)
    return f"<pre>{esc(body)}</pre>"


def _pnl_icon(details: str) -> str:
    match = _PNL_RE.search(details)
    if not match:
        return ""
    return "📈" if float(match.group(1)) >= 0 else "📉"


def alert_error(source: str, message: str) -> None:
    send_telegram_alert(f"🔴 <b>Error</b> — <code>{esc(source)}</code>\n{_format_body(message)}")


def alert_trade_opened(strategy_name: str, mode: str, symbol: str, details: str) -> None:
    icon = "🧪" if mode == "paper" else "✅"
    mode_label = "PAPER" if mode == "paper" else "LIVE"
    send_telegram_alert(
        f"{icon} <b>Trade Opened</b> <code>{mode_label}</code>\n"
        f"<b>{esc(strategy_name)}</b> · {esc(symbol)}\n"
        f"{_format_body(details)}"
    )


def alert_trade_closed(strategy_name: str, mode: str, symbol: str, details: str) -> None:
    icon = "🧪" if mode == "paper" else "✅"
    mode_label = "PAPER" if mode == "paper" else "LIVE"
    pnl_icon = _pnl_icon(details)
    header_suffix = f" {pnl_icon}" if pnl_icon else ""
    send_telegram_alert(
        f"{icon} <b>Trade Closed</b> <code>{mode_label}</code>{header_suffix}\n"
        f"<b>{esc(strategy_name)}</b> · {esc(symbol)}\n"
        f"{_format_body(details)}"
    )


def alert_manual_intervention(message: str) -> None:
    send_telegram_alert(f"🚨 <b>MANUAL INTERVENTION REQUIRED</b>\n{_format_body(message)}")


def alert_heartbeat(modes: dict, open_position_count: int, market_open: bool) -> None:
    """
    Once-a-day "still alive" ping — see run_daemon.py's heartbeat marker.
    Trade/error alerts only fire on activity, so a hung or crashed daemon
    on an otherwise-quiet day (no trades, no errors) would look identical
    to "nothing happened, all fine" from the outside. This is the signal
    that the process itself is actually up and ticking.
    """
    strategies_line = ", ".join(f"{esc(name)} ({esc(mode)})" for name, mode in modes.items()) or "none configured"
    body = (
        f"Strategies: {strategies_line}\n"
        f"Open positions: {open_position_count}\n"
        f"Market open right now: {'yes' if market_open else 'no'}"
    )
    send_telegram_alert(f"💓 <b>Daemon Heartbeat</b> — still running\n<pre>{body}</pre>")
