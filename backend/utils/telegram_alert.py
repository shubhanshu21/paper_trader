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

from config import TelegramConfig

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
# one of two shapes: " | "-joined summary fields (e.g. "SELL CE 24600@127.60
# | BUY CE 25000@17.65 | qty=50"), or space-separated "key=value" tokens
# (e.g. "position_id=1 strategy=Foo symbol=NIFTY"). _split_fields reflows
# both into one field per line; _format_body then classifies each field —
# "ACTION TYPE STRIKE@PRICE" fields become an aligned leg grid (icon +
# padded columns), recognized key=value fields (qty/reason/pnl/expiry)
# become labeled rows below it, and anything else (free text, an unrecognized
# key=value, or a field with an unexpected prefix glued on) is left as its
# own plain line rather than dropped — degrade gracefully, never silently
# lose content.
_FIELD_BOUNDARY_RE = re.compile(r"\s(?=\w+=)")
_LEG_RE = re.compile(r"^(BUY|SELL)\s+(CE|PE)\s+([\d.]+)@([\d.]+)$")
_KV_RE = re.compile(r"^(\w+)=(.+)$")
_PNL_RE = re.compile(r"pnl=([+-]?\d+(?:\.\d+)?)%")
_KV_LABELS = {"qty": "Qty", "reason": "Reason", "pnl": "P&L", "expiry": "Expiry"}
_DIVIDER = "─" * 24


def _split_fields(details: str) -> list[str]:
    body = details.replace(" | ", "\n")
    body = _FIELD_BOUNDARY_RE.sub("\n", body)
    return [f.strip() for f in body.split("\n") if f.strip()]


def _format_body(details: str) -> str:
    leg_rows: list[tuple[str, str, str, str]] = []
    kv_rows: list[tuple[str, str]] = []
    plain_rows: list[str] = []
    for field in _split_fields(details):
        leg_match = _LEG_RE.match(field)
        if leg_match:
            leg_rows.append(leg_match.groups())
            continue
        kv_match = _KV_RE.match(field)
        if kv_match and kv_match.group(1) in _KV_LABELS:
            key, value = kv_match.groups()
            kv_rows.append((_KV_LABELS[key], value))  # every real caller's "pnl=" value already includes its own trailing "%"
            continue
        plain_rows.append(field)

    lines: list[str] = []
    if leg_rows:
        strike_w = max(len(strike) for _, _, strike, _ in leg_rows)
        for action, option_type, strike, price in leg_rows:
            icon = "🟢" if action == "BUY" else "🔴"
            lines.append(f"{icon} {action:<4} {option_type}  {strike:>{strike_w}} @ ₹{price}")
    if leg_rows and (kv_rows or plain_rows):
        lines.append("")
    if kv_rows:
        label_w = max(len(label) for label, _ in kv_rows)
        lines.extend(f"{label:<{label_w}}  {value}" for label, value in kv_rows)
    lines.extend(plain_rows)

    body = "\n".join(lines) if lines else details
    # A bare <pre> block with no language gets run through the Telegram
    # CLIENT's own auto-detect-and-syntax-highlight heuristic (confirmed
    # Telegram-app behavior, not something the Bot API or this message
    # controls — see telegramdesktop/tdesktop#250) — it was guessing "Sql"
    # for lines like "SELL CE 24600.00@127.60" and slapping on a language
    # label + syntax colors neither useful nor accurate for what's really
    # just a plain field dump. Explicitly declaring language-text tells the
    # client this was already classified by the sender, which stops it
    # from overriding with its own guess.
    return f'<pre><code class="language-text">{esc(body)}</code></pre>'


def _pnl_icon(details: str) -> str:
    match = _PNL_RE.search(details)
    if not match:
        return ""
    return "📈" if float(match.group(1)) >= 0 else "📉"


def _card(icon: str, title: str, mode_label: str, strategy_name: str, symbol: str, details: str, pnl_icon: str = "") -> str:
    header_suffix = f" {pnl_icon}" if pnl_icon else ""
    return (
        f"{icon} <b>{title}</b> · <code>{mode_label}</code>{header_suffix}\n"
        f"{_DIVIDER}\n"
        f"<b>{esc(strategy_name)}</b> · {esc(symbol)}\n\n"
        f"{_format_body(details)}"
    )


def alert_error(source: str, message: str) -> None:
    send_telegram_alert(f"🔴 <b>Error</b> · <code>{esc(source)}</code>\n{_DIVIDER}\n{_format_body(message)}")


def alert_trade_opened(strategy_name: str, mode: str, symbol: str, details: str) -> None:
    icon = "🧪" if mode == "paper" else "✅"
    mode_label = "PAPER" if mode == "paper" else "LIVE"
    send_telegram_alert(_card(icon, "Trade Opened", mode_label, strategy_name, symbol, details))


def alert_trade_closed(strategy_name: str, mode: str, symbol: str, details: str) -> None:
    icon = "🧪" if mode == "paper" else "✅"
    mode_label = "PAPER" if mode == "paper" else "LIVE"
    send_telegram_alert(_card(icon, "Trade Closed", mode_label, strategy_name, symbol, details, _pnl_icon(details)))


def alert_pnl_update(strategy_name: str, mode: str, symbol: str, details: str) -> None:
    """Periodic while-open P&L snapshot — see custom_strategy_scheduler.py's _send_pnl_updates (distinct from alert_trade_closed, which fires once, at actual exit)."""
    icon = "🧪" if mode == "paper" else "✅"
    mode_label = "PAPER" if mode == "paper" else "LIVE"
    send_telegram_alert(_card(f"{icon} 📊", "P&amp;L Update", mode_label, strategy_name, symbol, details, _pnl_icon(details)))


def alert_manual_intervention(message: str) -> None:
    send_telegram_alert(f"🚨 <b>MANUAL INTERVENTION REQUIRED</b>\n{_DIVIDER}\n{_format_body(message)}")


def alert_heartbeat(modes: dict, open_position_count: int, market_open: bool) -> None:
    """
    Once-a-day "still alive" ping — see run_daemon.py's heartbeat marker.
    Trade/error alerts only fire on activity, so a hung or crashed daemon
    on an otherwise-quiet day (no trades, no errors) would look identical
    to "nothing happened, all fine" from the outside. This is the signal
    that the process itself is actually up and ticking.
    """
    strategies_line = ", ".join(f"{esc(name)} ({esc(mode)})" for name, mode in modes.items()) or "none configured"
    rows = [
        ("Strategies", strategies_line),
        ("Open positions", str(open_position_count)),
        ("Market open now", "yes" if market_open else "no"),
    ]
    label_w = max(len(label) for label, _ in rows)
    body = "\n".join(f"{label:<{label_w}}  {value}" for label, value in rows)
    send_telegram_alert(
        f"💓 <b>Daemon Heartbeat</b> — still running\n{_DIVIDER}\n"
        f'<pre><code class="language-text">{body}</code></pre>'
    )
