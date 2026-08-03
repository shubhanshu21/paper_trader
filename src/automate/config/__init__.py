"""
config.py — Centralised configuration loader for the Upstox trading bot.

All sensitive credentials are loaded exclusively from environment variables
(via a .env file). This module errors out immediately if any required key is
missing, preventing the strategy from running with incomplete configuration.

Security note: No hardcoded secrets or fallback literals are used anywhere.
"""

import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Load .env file from the project root. src/automate/config/__init__.py -> up 4
# levels (config -> automate -> src -> repo root).
# Anchored to this file's real location (not cwd), so this works
# regardless of where a script/cron job happens to be invoked from.
# ---------------------------------------------------------------------------
_ENV_FILE = Path(__file__).resolve().parent.parent.parent.parent / ".env"
if _ENV_FILE.exists():
    load_dotenv(dotenv_path=_ENV_FILE)
else:
    load_dotenv()  # Fallback: read from shell environment (CI/cron)


def _optional(key: str, default: str = "") -> str:
    """Fetch an optional env var with a safe default."""
    return os.environ.get(key, default).strip()


# ---------------------------------------------------------------------------
# Upstox credentials — the one and only real broker account.
# ---------------------------------------------------------------------------
def _load_access_token() -> str:
    """
    ACCESS_TOKEN is intentionally NOT a plain env-backed attribute like the
    others: it's a daily-expiring OAuth token that gets rewritten by the
    auto-login/manual-login flows, and is read by two independent OS
    processes (automate-api, run_daemon.py). Storing it in MySQL (via
    auth.token_store) instead of .env means a refresh in one process is
    immediately visible to the other on next access, instead of requiring
    both to be manually restarted. Falls back to empty (never raises) if
    the DB isn't reachable yet — callers already handle a missing token via
    UpstoxConfig.validate().
    """
    try:
        from automate.auth.token_store import get_access_token
        return get_access_token() or ""
    except Exception:
        logging.getLogger("config").warning("Could not read Upstox token from DB.", exc_info=True)
        return ""


def _save_access_token(token: str) -> None:
    from automate.auth.token_store import set_access_token
    set_access_token(token)


class _UpstoxConfigMeta(type):
    """Backs UpstoxConfig.ACCESS_TOKEN with a DB read/write instead of a
    static class attribute, while every existing call site keeps using the
    exact same `UpstoxConfig.ACCESS_TOKEN` / `UpstoxConfig.ACCESS_TOKEN = x`
    syntax unchanged."""

    @property
    def ACCESS_TOKEN(cls) -> str:
        return _load_access_token()

    @ACCESS_TOKEN.setter
    def ACCESS_TOKEN(cls, value: str) -> None:
        _save_access_token(value)


class UpstoxConfig(metaclass=_UpstoxConfigMeta):
    """Upstox OAuth2 credentials."""
    API_KEY: str        = _optional("UPSTOX_API_KEY")
    API_SECRET: str     = _optional("UPSTOX_API_SECRET")
    REDIRECT_URI: str   = _optional("UPSTOX_REDIRECT_URI", "https://127.0.0.1/")
    # ACCESS_TOKEN is provided by _UpstoxConfigMeta above (DB-backed, not .env).

    # Optional — only needed for auth/upstox_auto_login.py's headless daily
    # token refresh. SECURITY: unlike ACCESS_TOKEN (daily-expiring,
    # revocable, API-scoped), TOTP_SECRET is the seed your 2FA app uses to
    # generate codes — anyone with it + PIN has permanent full login access
    # to the real account, indefinitely. Leave blank to keep using the
    # manual `python3 -m auth.upstox_auth` flow instead (auto-login is
    # skipped entirely if any of these three are unset).
    USERNAME: str       = _optional("UPSTOX_USERNAME")     # mobile number
    PIN: str            = _optional("UPSTOX_PIN")
    TOTP_SECRET: str    = _optional("UPSTOX_TOTP_SECRET")

    @classmethod
    def validate(cls) -> None:
        """Raise SystemExit if Upstox credentials are incomplete."""
        for attr, key, source in [
            ("API_KEY", "UPSTOX_API_KEY", "env var"),
            ("API_SECRET", "UPSTOX_API_SECRET", "env var"),
            ("ACCESS_TOKEN", "broker_tokens DB row (broker='upstox')", "DB"),
        ]:
            if not getattr(cls, attr):
                logging.critical("Missing required %s '%s' for Upstox broker.", source, key)
                sys.exit(1)

    @classmethod
    def auto_login_configured(cls) -> bool:
        return bool(cls.USERNAME and cls.PIN and cls.TOTP_SECRET)


# ---------------------------------------------------------------------------
# Run parameters
# ---------------------------------------------------------------------------
class RunConfig:
    """Global execution parameters."""
    # Which strategies (by strategies/registry.py key) run_strategy.py
    # actually executes. This is an explicit opt-in list, NOT "run
    # everything in the registry" — the registry also serves backtesting,
    # so adding a strategy there for backtest purposes must not silently
    # make it go live too. Comma-separated in .env; --strategies on the
    # CLI overrides this for a one-off run.
    ACTIVE_STRATEGIES: list[str] = [
        s.strip() for s in _optional("ACTIVE_STRATEGIES", "ten_percent_otm_strangle").split(",") if s.strip()
    ]


# ---------------------------------------------------------------------------
# Strategy Configurations
# ---------------------------------------------------------------------------
class TenPercentOTMStrangleConfig:
    """Specific configuration for the Short Strangle strategy."""
    
    # List of symbols to trade for this strategy
    SYMBOLS: list[str] = ["RELIANCE", "TCS"]

    # How many lots to sell per leg. Actual order quantity = NUM_LOTS ×
    # LOT_SIZES[symbol] (the real NSE contract multiplier below) — NOT this
    # number directly. Previously this field was misleadingly named
    # LOT_SIZE but was actually used as raw quantity (i.e. "1 lot" meant
    # 1 share, not a real lot) everywhere it was read — that bug is fixed
    # by this rename; every caller now resolves the real per-symbol lot
    # size instead of trusting a caller-supplied "quantity".
    NUM_LOTS: int = 1

    # Product type: 'NRML' (overnight) or 'MIS' (intraday)
    PRODUCT: str = "NRML"

    # 'paper'  → orders are simulated against real live market data (via
    #            PaperBroker) — no real money, no real orders, ever.
    # 'live'   → orders are actually placed with your real Upstox account.
    #            No separate dry-run gate on top of this — live means live.
    # This is deliberately per-strategy, not a global setting: a new
    # strategy should default to 'paper' and prove itself before anyone
    # flips it to 'live', without touching any already-live strategy.
    MODE: str = "paper"

    # Extra stock symbols worth having real historical candles for (see
    # scripts/download_real_history.py's --all), beyond what's actually
    # in SYMBOLS today. NOT a strike-step map anymore — strike_step is
    # resolved dynamically now (see below and TenPercentOTMStrangle.
    # __init__()), same reasoning as lot size: a hardcoded strike-interval
    # table has the exact same staleness risk, and it was already proven
    # real — several of these had already drifted (e.g. RELIANCE was 20,
    # really 10; TCS was 50, really 20) the moment they were checked
    # against real listed strikes.
    KNOWN_STOCK_SYMBOLS: list[str] = [
        "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "SBIN",
        "AXISBANK", "KOTAKBANK", "BAJFINANCE", "LT", "WIPRO",
    ]

    # Known indices — not in SYMBOLS by default (indices need weekly-
    # expiry-aware handling this strategy doesn't implement), but used by
    # scripts/download_real_history.py's --all bulk download so backtest
    # data is available for them too.
    KNOWN_INDEX_SYMBOLS: list[str] = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"]

    # Post-entry risk controls, as a % of premium collected (see
    # utils/option_utils.py's strangle_pnl_pct/check_exit_trigger and
    # TenPercentOTMStrangle's docstring). DISABLED (None) by default — hold
    # to expiry unconditionally, the original behavior. This is deliberate,
    # not an oversight: backtesting real RELIANCE data over a SHORT window
    # (6 cycles containing one big loss) made a stop-loss look like a clear
    # win, but widening to a longer, more representative window (19 cycles)
    # reversed that — no-stop-loss won on total return (+₹27,276 / 19.21%)
    # against every stop-loss tried (best was 40%, +₹21,173 / 14.91%,
    # win rate dropped from 95% to 74% because it also cuts short trades
    # that would have recovered by expiry). A stop-loss trades away some
    # average return for a smaller worst case (-₹4,583 vs -₹19,280) — a
    # real preference, not a strict improvement, so enabling it is a
    # deliberate edit here, same as SYMBOLS/STRIKE_STEPS above, not an env
    # var — this IS the strategy's own config, no reason to split it out.
    TAKE_PROFIT_PCT: float | None = None
    STOP_LOSS_PCT: float | None = None

    # Always exit at least this many days before the option's own expiry —
    # never hold into expiry day itself, regardless of SL/TP state. This is
    # a hard floor, separate from (and in addition to) the SL/TP triggers
    # above: it exists specifically to stay clear of expiry-day gamma risk
    # and, for stock options, the compulsory physical-settlement risk (see
    # run_position_monitor.py's module docstring) — not to lock in profit.
    # 1 day is enough to fully avoid both while still capturing nearly the
    # entire month's time-decay; the actual close happens on the nearest
    # real trading day on/after this date, since monitoring only ever runs
    # on trading days anyway.
    EXIT_DAYS_BEFORE_EXPIRY: int = 1

    # Extra constructor kwargs beyond the shared symbol/num_lots/strike_step
    # /product ones.
    EXTRA_KWARGS: dict = {
        "take_profit_pct": TAKE_PROFIT_PCT,
        "stop_loss_pct": STOP_LOSS_PCT,
        "exit_days_before_expiry": EXIT_DAYS_BEFORE_EXPIRY,
    }


# ---------------------------------------------------------------------------
# Telegram alerts (optional — token expiry, trades taken, failures/errors)
# ---------------------------------------------------------------------------
class TelegramConfig:
    """
    Optional operational alerting. Unset by default — utils/telegram_alert.py
    silently no-ops (logs once) if either is blank, so this is never a hard
    requirement to run the bot, just a recommended one.
    Get BOT_TOKEN from @BotFather; get CHAT_ID by messaging your bot once and
    checking https://api.telegram.org/bot<TOKEN>/getUpdates.
    """
    BOT_TOKEN: str = _optional("TELEGRAM_BOT_TOKEN")
    CHAT_ID: str = _optional("TELEGRAM_CHAT_ID")

    @classmethod
    def is_configured(cls) -> bool:
        return bool(cls.BOT_TOKEN and cls.CHAT_ID)


# ---------------------------------------------------------------------------
# Market Hours
# ---------------------------------------------------------------------------
class MarketConfig:
    """Market Hours in HH:MM:SS format"""
    OPEN: str = _optional("MARKET_OPEN", "09:15:00")
    CLOSE: str = _optional("MARKET_CLOSE", "15:30:00")


# ---------------------------------------------------------------------------
# Logging configuration
# ---------------------------------------------------------------------------
class LogConfig:
    LEVEL: str = _optional("LOG_LEVEL", "INFO").upper()
    FILE: str  = _optional("LOG_FILE", "logs/strategy.log")


# ---------------------------------------------------------------------------
# MySQL Database — production runtime store for positions + backtest_runs +
# fno_bhavcopy (replaces the former data/runtime/trading.db SQLite file).
# ---------------------------------------------------------------------------
class DatabaseConfig:
    """MySQL connection settings — all loaded from env, never hardcoded."""
    HOST:     str = _optional("DB_HOST", "127.0.0.1")
    PORT:     int = int(_optional("DB_PORT", "3306"))
    USER:     str = _optional("DB_USER")
    PASSWORD: str = _optional("DB_PASSWORD")
    NAME:     str = _optional("DB_NAME", "automate")

    @classmethod
    def validate(cls) -> None:
        """Exit immediately if required DB credentials are missing."""
        if not cls.USER:
            logging.critical("Missing required env var 'DB_USER' for MySQL.")
            sys.exit(1)
        if not cls.PASSWORD:
            logging.critical("Missing required env var 'DB_PASSWORD' for MySQL.")
            sys.exit(1)

    @classmethod
    def url(cls) -> str:
        """
        Build a PyMySQL connection URL from env vars.
        Never call this before validate() — PASSWORD may be empty.
        The URL is never logged (engine is created with hide_parameters=True).
        """
        import urllib.parse
        pwd = urllib.parse.quote_plus(cls.PASSWORD)
        return (
            f"mysql+pymysql://{cls.USER}:{pwd}"
            f"@{cls.HOST}:{cls.PORT}/{cls.NAME}"
            f"?charset=utf8mb4"
        )


# ---------------------------------------------------------------------------
# Panel Auth (optional — disabled by default for backward compat)
# ---------------------------------------------------------------------------
class PanelAuthConfig:
    """
    Web panel authentication gate.

    Set PANEL_AUTH_ENABLED=true to require login before using the control
    panel. When disabled (default), behaviour is unchanged — localhost-only,
    no auth layer, same as before.

    JWT secret resolution (multi-tiered, never hardcoded):
      1. PANEL_JWT_SECRET env var
      2. .panel_jwt_secret file in project root
      3. Ephemeral random secret
    """
    ENABLED: bool = _optional("PANEL_AUTH_ENABLED", "false").lower() in ("true", "1", "yes")
    SESSION_HOURS: int = int(_optional("PANEL_SESSION_HOURS", "8"))
    OPEN_REGISTRATION: bool = _optional("PANEL_OPEN_REGISTRATION", "true").lower() in ("true", "1", "yes")

    @classmethod
    def jwt_secret(cls) -> str:
        """Resolve JWT signing secret using the multi-tiered strategy."""
        import secrets as _secrets
        env_val = os.environ.get("PANEL_JWT_SECRET", "").strip()
        if env_val:
            return env_val
        
        secret_file = Path(__file__).resolve().parent.parent.parent.parent / ".panel_jwt_secret"
        if secret_file.exists():
            return secret_file.read_text().strip()
        
        logging.warning(
            "PANEL_JWT_SECRET not set — generating ephemeral secret. "
            "Sessions will be lost on restart and won't work across multiple instances."
        )
        return _secrets.token_hex(32)


# ---------------------------------------------------------------------------
# Google OAuth — optional alternative login for the web panel
# ---------------------------------------------------------------------------
class GoogleOAuthConfig:
    """
    "Sign in with Google" for the panel (api/routes_oauth.py) — entirely
    optional; the panel works exactly as before with only username/
    password if this isn't configured.

    Requires a real OAuth 2.0 Client ID registered in Google Cloud
    Console (APIs & Services -> Credentials -> Create Credentials ->
    OAuth client ID -> Web application), with GOOGLE_OAUTH_REDIRECT_URI
    added to that client's "Authorized redirect URIs". This app cannot
    create that registration for you — it's tied to your own Google
    Cloud project/billing identity.

    Set:
      GOOGLE_OAUTH_CLIENT_ID
      GOOGLE_OAUTH_CLIENT_SECRET
      GOOGLE_OAUTH_REDIRECT_URI (e.g. https://your-domain/api/auth/oauth/google/callback)
    """
    CLIENT_ID:     str = _optional("GOOGLE_OAUTH_CLIENT_ID")
    CLIENT_SECRET: str = _optional("GOOGLE_OAUTH_CLIENT_SECRET")
    REDIRECT_URI:  str = _optional("GOOGLE_OAUTH_REDIRECT_URI")

    @classmethod
    def is_configured(cls) -> bool:
        return bool(cls.CLIENT_ID and cls.CLIENT_SECRET and cls.REDIRECT_URI)


# ---------------------------------------------------------------------------
# Equity Moving Average Crossover Strategy
# ---------------------------------------------------------------------------
class EquityMACrossoverConfig:
    """Configuration for the equity MA-crossover strategy."""
    SYMBOLS: list[str] = [
        s.strip()
        for s in _optional("EQUITY_MA_SYMBOLS", "RELIANCE,TCS").split(",")
        if s.strip()
    ]
    SHORT_WINDOW: int = int(_optional("EQUITY_MA_SHORT_WINDOW", "10"))
    LONG_WINDOW: int = int(_optional("EQUITY_MA_LONG_WINDOW", "30"))
    NUM_SHARES: int = int(_optional("EQUITY_NUM_SHARES", "1"))
    PRODUCT: str = _optional("EQUITY_PRODUCT", "CNC")
    MODE: str = _optional("EQUITY_MODE", "paper")


# ---------------------------------------------------------------------------
# Per-strategy config lookup
# ---------------------------------------------------------------------------
STRATEGY_CONFIGS = {
    "ten_percent_otm_strangle": TenPercentOTMStrangleConfig,
    "equity_ma_crossover": EquityMACrossoverConfig,
}
