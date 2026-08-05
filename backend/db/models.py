"""
db/models.py — SQLAlchemy ORM models for all runtime tables.

Tables covered:
  - positions       (utils/position_tracker.py)
  - backtest_runs   (utils/backtest_history.py)
  - fno_bhavcopy    (backtest/bhavcopy_data_feed.py — historical dataset)
  - candles         (backtest/data_feed.py — intraday minute candles)

Column types chosen for MySQL production:
  - DECIMAL(12,4) for prices  → exact arithmetic, no IEEE float rounding
  - VARCHAR with explicit lengths → enables proper B-tree indexing
  - DATETIME                  → timezone-naive (all IST, matching existing data)
  - BIGINT AUTO_INCREMENT PK  → future-proof vs INT overflow

Security: No raw SQL strings used here — all access via ORM queries or
  parameterized text() calls in the caller modules.
"""
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    func,
)

from db.engine import Base


# ---------------------------------------------------------------------------
# Runtime: trading positions
# ---------------------------------------------------------------------------
class Position(Base):
    __tablename__ = "positions"

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    # NULL = created outside any HTTP request (the legacy CLI daemon has no
    # per-request user context at all — see cli/run_daemon.py) — treated as
    # system-wide/admin-only, same convention as Notification.user_id.
    # A manually-placed terminal trade DOES have a real owner (see
    # routes_terminal.py's POST /trade) and gets a real user_id.
    user_id          = Column(BigInteger, nullable=True)
    strategy_name    = Column(String(128), nullable=False)
    mode             = Column(String(8),   nullable=False, default="paper")   # 'paper' | 'live'
    symbol           = Column(String(32),  nullable=False)
    entry_date       = Column(String(10),  nullable=False)   # ISO date YYYY-MM-DD
    expiry           = Column(String(10),  nullable=False)

    call_token       = Column(String(256), nullable=False)
    call_strike      = Column(Integer,     nullable=False)
    call_entry_price = Column(Numeric(12, 4), nullable=False)
    call_order_id    = Column(String(64),  nullable=True)

    put_token        = Column(String(256), nullable=False)
    put_strike       = Column(Integer,     nullable=False)
    put_entry_price  = Column(Numeric(12, 4), nullable=False)
    put_order_id     = Column(String(64),  nullable=True)

    quantity         = Column(Integer,     nullable=False)
    product          = Column(String(8),   nullable=False)  # NRML | MIS
    take_profit_pct  = Column(Numeric(8, 4), nullable=True)
    stop_loss_pct    = Column(Numeric(8, 4), nullable=True)
    exit_days_before_expiry = Column(SmallInteger, nullable=False, default=1)

    status           = Column(String(8),   nullable=False, default="OPEN")  # OPEN | CLOSED
    exit_date        = Column(String(10),  nullable=True)
    exit_reason      = Column(String(32),  nullable=True)

    call_exit_price      = Column(Numeric(12, 4), nullable=True)
    put_exit_price       = Column(Numeric(12, 4), nullable=True)
    call_exit_order_id   = Column(String(64),  nullable=True)
    put_exit_order_id    = Column(String(64),  nullable=True)

    __table_args__ = (
        Index("ix_positions_status",               "status"),
        Index("ix_positions_strategy_symbol_status", "strategy_name", "symbol", "status"),
        Index("ix_positions_mode_status",           "mode", "status"),
        Index("ix_positions_user_id",               "user_id"),
    )

    def to_dict(self):
        from datetime import datetime
        from decimal import Decimal

        def _serialize(v):
            if isinstance(v, Decimal):
                return float(v)
            if isinstance(v, datetime):
                return v.isoformat()
            return v

        return {
            c.name: _serialize(v)
            for c, v in zip(self.__table__.columns, [
                getattr(self, c.name) for c in self.__table__.columns
            ], strict=False)
        }


# ---------------------------------------------------------------------------
# Runtime: backtest history
# ---------------------------------------------------------------------------
class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    id              = Column(BigInteger, primary_key=True, autoincrement=True)
    run_at          = Column(String(26), nullable=False)   # ISO datetime string
    strategy_name   = Column(String(128), nullable=False)
    symbol          = Column(String(32),  nullable=False)
    contract_type   = Column(String(16),  nullable=False)
    from_date       = Column(String(10),  nullable=False)
    to_date         = Column(String(10),  nullable=False)
    cycles          = Column(Integer,     nullable=False)
    wins            = Column(Integer,     nullable=False)
    win_rate_pct    = Column(Numeric(6, 2), nullable=True)
    total_pnl       = Column(Numeric(16, 4), nullable=False)
    total_return_pct = Column(Numeric(10, 4), nullable=True)

    __table_args__ = (
        Index("ix_backtest_runs_strategy_symbol", "strategy_name", "symbol"),
        Index("ix_backtest_runs_run_at",          "run_at"),
    )


# ---------------------------------------------------------------------------
# Historical dataset: NSE F&O daily bhavcopy
# ---------------------------------------------------------------------------
class FnoBhavcopy(Base):
    __tablename__ = "fno_bhavcopy"

    # Composite PK avoids an extra surrogate column on a 35GB table —
    # (trade_date, symbol, instrument, expiry_dt, strike_pr, option_typ)
    # is naturally unique for any real EOD bhavcopy row.
    id           = Column(BigInteger, primary_key=True, autoincrement=True)

    instrument   = Column(String(8),   nullable=False)
    symbol       = Column(String(32),  nullable=False)
    expiry_dt    = Column(String(10),  nullable=True)   # YYYY-MM-DD | NULL for futures
    strike_pr    = Column(Numeric(12, 2), nullable=True)
    option_typ   = Column(String(2),   nullable=True)   # CE | PE | NULL for futures
    open         = Column(Numeric(12, 4), nullable=True)
    high         = Column(Numeric(12, 4), nullable=True)
    low          = Column(Numeric(12, 4), nullable=True)
    close        = Column(Numeric(12, 4), nullable=True)
    settle_pr    = Column(Numeric(12, 4), nullable=True)
    contracts    = Column(Integer,     nullable=True)
    val_inlakh   = Column(Numeric(16, 4), nullable=True)
    open_int     = Column(BigInteger,  nullable=True)  # some contracts exceed INT's 2.1B cap (e.g. IDEA FUTSTK)
    chg_in_oi    = Column(BigInteger,  nullable=True)
    trade_date   = Column(String(10),  nullable=False)  # YYYY-MM-DD

    __table_args__ = (
        # Mirrors the 4 SQLite indexes from the original bhavcopy.db
        Index("ix_bhav_symbol_instrument_date",         "symbol", "instrument", "trade_date"),
        Index("ix_bhav_symbol_instrument_expiry",       "symbol", "instrument", "expiry_dt"),
        Index("ix_bhav_symbol_instrument_expiry_date",  "symbol", "instrument", "expiry_dt", "trade_date"),
    )


# ---------------------------------------------------------------------------
# Runtime: paper-trading wallet settings (single row, id=1)
# ---------------------------------------------------------------------------
class WalletSettings(Base):
    """
    One row per user's virtual paper-trading wallet (used to be a single
    global singleton row, id always 1 — see migration 0015, which added
    user_id and backfilled the one pre-existing row to the sole admin
    account at the time). get_or_create semantics now: a brand-new user
    gets their own row lazily on first wallet access, not a shared one.
    """
    __tablename__ = "wallet_settings"

    id               = Column(SmallInteger, primary_key=True, autoincrement=True)
    user_id          = Column(BigInteger, nullable=True, unique=True)
    starting_capital = Column(Numeric(16, 2), nullable=False, server_default="0")

    # Per-user F&O transaction cost overrides (see utils/costs.py's
    # DEFAULT_RATES) — NULL means "use the codebase default for that
    # component", so a brand-new row (or a column added by a later
    # migration) doesn't need a backfill to keep working. Edited from the
    # Profile page whenever NSE/SEBI/govt revise a rate, without a deploy.
    brokerage_per_order = Column(Numeric(10, 2), nullable=True)
    exchange_charge_pct = Column(Numeric(12, 8), nullable=True)
    gst_pct             = Column(Numeric(6, 4), nullable=True)
    stt_pct             = Column(Numeric(6, 4), nullable=True)
    sebi_charge_pct     = Column(Numeric(12, 8), nullable=True)
    stamp_duty_pct      = Column(Numeric(12, 8), nullable=True)


# ---------------------------------------------------------------------------
# Panel authentication: registered users
# ---------------------------------------------------------------------------
class User(Base):
    """
    Web panel user account.

    Security:
    - Passwords are never stored in plaintext — only bcrypt hashes.
    - role: 'admin' can create/manage users; 'viewer' is read-only.
    - is_active: deactivated accounts cannot log in. Combined with
      token_version below, a deactivation now also kills any session that
      was already issued before the deactivation, not just future logins.
    - token_version: embedded in every issued JWT as the 'tv' claim (see
      api/auth.py::create_access_token) and checked against this column on
      every authenticated request. Bumping it (api/auth.py::bump_token_version)
      immediately invalidates every token issued before the bump, even
      though JWTs are otherwise stateless and normally valid until 'exp' —
      used for "log out everywhere" and admin-forced deactivation.
    """
    __tablename__ = "panel_users"

    id             = Column(BigInteger, primary_key=True, autoincrement=True)
    username       = Column(String(64),  nullable=False, unique=True)
    email          = Column(String(254), nullable=False, unique=True)
    hashed_password = Column(String(256), nullable=False)
    role           = Column(String(16),  nullable=False, default="viewer")   # 'admin' | 'viewer'
    is_active      = Column(Integer,     nullable=False, default=1)           # 0 = deactivated
    token_version  = Column(Integer,     nullable=False, default=0)           # bumped to revoke all outstanding sessions
    created_at     = Column(DateTime,    nullable=False, server_default=func.now())

    # TOTP two-factor auth (see utils/mfa.py) — mfa_secret is only set once
    # the user has proven they scanned it (submitted one valid code back
    # during enrollment); mfa_backup_codes_json holds bcrypt-hashed,
    # single-use recovery codes for when the authenticator device is lost.
    mfa_enabled           = Column(Integer, nullable=False, default=0)
    mfa_secret             = Column(String(64), nullable=True)
    mfa_backup_codes_json   = Column(Text, nullable=True)

    __table_args__ = (
        Index("ix_panel_users_username", "username"),
        Index("ix_panel_users_email",    "email"),
    )

    def to_safe_dict(self):
        """Return user data safe to send to the client (no hashed_password)."""
        return {
            "id":         self.id,
            "username":   self.username,
            "email":      self.email,
            "role":       self.role,
            "is_active":  bool(self.is_active),
            "mfa_enabled": bool(self.mfa_enabled),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


# ---------------------------------------------------------------------------
# Equity trading positions
# ---------------------------------------------------------------------------
class EquityPosition(Base):
    """
    A single equity (spot/CNC or intraday/MIS) position opened by an equity
    strategy. Separate from the options-only Position table above.

    direction: 'LONG' (buy first, sell to exit) or 'SHORT' (not implemented
               in the initial MA-crossover strategy — reserved for future use).
    """
    __tablename__ = "equity_positions"

    id             = Column(BigInteger,    primary_key=True, autoincrement=True)
    # Same NULL = system-wide/no-request-owner convention as Position.user_id above.
    user_id        = Column(BigInteger,    nullable=True)
    strategy_name  = Column(String(128),   nullable=False)
    mode           = Column(String(8),     nullable=False, default="paper")  # 'paper' | 'live'
    symbol         = Column(String(32),    nullable=False)
    direction      = Column(String(8),     nullable=False, default="LONG")   # 'LONG' | 'SHORT'
    product        = Column(String(8),     nullable=False, default="CNC")    # 'CNC' | 'MIS'

    entry_date     = Column(String(10),    nullable=False)   # ISO date YYYY-MM-DD
    entry_price    = Column(Numeric(12, 4), nullable=False)
    quantity       = Column(Integer,       nullable=False)
    entry_order_id = Column(String(64),    nullable=True)

    status         = Column(String(8),     nullable=False, default="OPEN")   # 'OPEN' | 'CLOSED'
    exit_date      = Column(String(10),    nullable=True)
    exit_price     = Column(Numeric(12, 4), nullable=True)
    exit_reason    = Column(String(64),    nullable=True)
    exit_order_id  = Column(String(64),    nullable=True)

    # Computed at close time and cached here to avoid re-querying prices.
    gross_pnl      = Column(Numeric(16, 4), nullable=True)
    net_pnl        = Column(Numeric(16, 4), nullable=True)
    charges        = Column(Numeric(12, 4), nullable=True)

    # Live mark-to-market for OPEN positions — written by the /ws/positions
    # background poller (ws_positions.py), read-only everywhere else.
    current_price    = Column(Numeric(12, 4), nullable=True)
    unrealized_pnl   = Column(Numeric(16, 4), nullable=True)
    price_updated_at = Column(DateTime,       nullable=True)

    __table_args__ = (
        Index("ix_equity_positions_status",          "status"),
        Index("ix_equity_positions_strategy_symbol", "strategy_name", "symbol", "status"),
        Index("ix_equity_positions_mode_status",     "mode", "status"),
        Index("ix_equity_positions_user_id",         "user_id"),
    )

    def to_dict(self):
        from datetime import datetime
        from decimal import Decimal

        def _serialize(v):
            if isinstance(v, Decimal):
                return float(v)
            if isinstance(v, datetime):
                return v.isoformat()
            return v

        return {
            c.name: _serialize(v)
            for c, v in zip(self.__table__.columns, [
                getattr(self, c.name) for c in self.__table__.columns
            ], strict=False)
        }


# ---------------------------------------------------------------------------
# Broker credentials (replaces storing UPSTOX_ACCESS_TOKEN in .env — a
# daily-expiring OAuth token that both the API process and the trading
# daemon need current, in-sync, plaintext-on-disk copies of. One row per
# broker; keyed by broker name since this app only integrates with Upstox
# today but the shape stays correct if that changes.
# ---------------------------------------------------------------------------
class BrokerToken(Base):
    __tablename__ = "broker_tokens"

    broker       = Column(String(32), primary_key=True)   # e.g. 'upstox'
    access_token = Column(Text,       nullable=True)
    updated_at   = Column(DateTime,   nullable=False, server_default=func.now(), onupdate=func.now())


# ---------------------------------------------------------------------------
# Seeded Master: Upstox Instruments (NSE, MCX, BSE)
# ---------------------------------------------------------------------------
class Instrument(Base):
    """
    Unified local master of all Upstox instruments (NSE spot, options, futures,
    and MCX commodities), synced daily. Used to provide fast, indexed searching.
    """
    __tablename__ = "instruments"

    instrument_key  = Column(String(64), primary_key=True)
    exchange_token  = Column(String(64), nullable=True)
    symbol          = Column(String(64), nullable=False)
    name            = Column(String(128), nullable=True)
    last_price      = Column(Numeric(12, 4), nullable=True)
    expiry          = Column(String(10), nullable=True)
    strike          = Column(Numeric(12, 2), nullable=True)
    tick_size       = Column(Numeric(12, 4), nullable=True)
    lot_size        = Column(Integer, nullable=True)
    instrument_type = Column(String(16), nullable=True)
    option_type     = Column(String(16), nullable=True)
    exchange        = Column(String(16), nullable=False)


    __table_args__ = (
        Index("ix_instruments_symbol", "symbol"),
        Index("ix_instruments_exchange", "exchange"),
    )

    def to_dict(self):
        from datetime import datetime
        from decimal import Decimal

        def _serialize(v):
            if isinstance(v, Decimal):
                return float(v)
            if isinstance(v, datetime):
                return v.isoformat()
            return v

        return {
            c.name: _serialize(v)
            for c, v in zip(self.__table__.columns, [
                getattr(self, c.name) for c in self.__table__.columns
            ], strict=False)
        }


# ---------------------------------------------------------------------------
# User watchlists
# ---------------------------------------------------------------------------
class UserWatchlist(Base):
    """
    User-specific watchlist for tracking instruments.
    Allows users to maintain custom watchlists that persist across sessions.
    """
    __tablename__ = "user_watchlists"

    id               = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id          = Column(BigInteger, nullable=False)  # References panel_users.id
    instrument_key   = Column(String(64), nullable=False)  # References instruments.instrument_key
    added_at         = Column(DateTime, nullable=False, server_default=func.now())
    order_index      = Column(Integer, nullable=True)  # For custom ordering
    page             = Column(Integer, nullable=False, default=1, server_default="1")

    __table_args__ = (
        Index("ix_user_watchlists_user_id", "user_id"),
        Index("ix_user_watchlists_instrument", "instrument_key"),
        Index("ux_user_watchlist_user_instrument_page", "user_id", "instrument_key", "page", unique=True),
    )

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "instrument_key": self.instrument_key,
            "added_at": self.added_at.isoformat() if self.added_at else None,
            "order_index": self.order_index,
            "page": self.page,
        }


# ---------------------------------------------------------------------------
# Custom Strategies
# ---------------------------------------------------------------------------
class CustomStrategy(Base):
    __tablename__ = "custom_strategies"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, nullable=True)
    name = Column(String(128), nullable=False)
    description = Column(Text, nullable=True)
    
    # Instrument selection
    instrument_type = Column(String(16), nullable=False)  # 'INDEX' | 'STOCK' | 'COMMODITY'
    symbols = Column(Text, nullable=False)  # JSON array of symbols
    
    # Strategy parameters
    strategy_type = Column(String(32), nullable=False)  # 'STRADDLE' | 'STRANGLE' | 'IRON_CONDOR' | 'BUTTERFLY' | 'CUSTOM'
    option_type = Column(String(8), nullable=False)  # 'CE' | 'PE' | 'BOTH'
    strike_offset = Column(Numeric(8, 4), nullable=True)  # Strike offset from ATM
    expiry_days = Column(Integer, nullable=True)  # Days to expiry
    
    # Risk management
    num_lots = Column(Integer, nullable=False, default=1)
    take_profit_pct = Column(Numeric(8, 4), nullable=True)
    stop_loss_pct = Column(Numeric(8, 4), nullable=True)
    exit_days_before_expiry = Column(Integer, nullable=False, default=1)
    
    # Deployment status
    status = Column(String(16), nullable=False, default="DRAFT")  # 'DRAFT' | 'BACKTESTING' | 'PAPER_TRADING' | 'LIVE' | 'PAUSED' | 'STOPPED'
    # 'PAPER_TRADING' | 'LIVE' snapshot of status right before the most
    # recent pause, so resume/{id} can restore the mode it was actually
    # running in instead of always dropping back to paper trading (a LIVE
    # strategy silently downgraded to paper on resume would look like a
    # success while real trading had actually stopped). Set on pause,
    # consumed and cleared on resume.
    pre_pause_status = Column(String(16), nullable=True)

    # Composable rule definition (legs / entry / exit) — see
    # strategies/custom/rule_schema.py for the JSON contract. This is what
    # actually drives execution now; strategy_type/option_type/strike_offset/
    # expiry_days above are kept only for backward compatibility with rows
    # created before this existed and are no longer written by new creates.
    rules_json = Column(Text, nullable=True)
    # 'YYYY-MM-DD' of the last day this strategy's entry logic actually ran
    # (paper/live) — prevents double-entry if the scheduler ticks twice in
    # one day.
    # NOT a plain 'YYYY-MM-DD' anymore despite the name (kept for backward
    # compat) — custom_strategy_scheduler.py's cycle-aware entry tracking
    # stores a per-symbol JSON blob here (see _get/_set_last_entered_expiry),
    # which routinely exceeds 10 chars — must stay Text, not VARCHAR(10)
    # (see migration 0014; a too-narrow column here silently rolled back
    # the entire entry transaction, including new positions, every time).
    last_entry_date = Column(Text, nullable=True)

    # Performance tracking
    backtest_return_pct = Column(Numeric(8, 4), nullable=True)
    paper_return_pct = Column(Numeric(8, 4), nullable=True)
    live_return_pct = Column(Numeric(8, 4), nullable=True)

    # Full backtest result (cycles_tested, win_rate_pct, avg_return_pct_of_premium,
    # from_date/to_date, and every per-cycle row) — persisted so the result can be
    # viewed anytime from the Strategies page, not just right after running.
    # Overwritten wholesale on every re-run for this strategy (one stored result
    # per strategy, not a history of runs).
    backtest_result_json = Column(Text, nullable=True)
    backtest_run_at = Column(DateTime, nullable=True)

    # Timestamps
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=True, onupdate=func.now())
    deployed_at = Column(DateTime, nullable=True)
    
    # Options automation settings
    auto_roll = Column(SmallInteger, nullable=False, default=0)  # 0/1 boolean
    roll_threshold_pct = Column(Numeric(8, 4), nullable=True)
    auto_adjust = Column(SmallInteger, nullable=False, default=0)  # 0/1 boolean
    greek_threshold_delta = Column(Numeric(8, 4), nullable=True)
    greek_threshold_theta = Column(Numeric(8, 4), nullable=True)

    __table_args__ = (
        Index("ix_custom_strategies_user_id", "user_id"),
        Index("ix_custom_strategies_status", "status"),
        Index("ix_custom_strategies_instrument_type", "instrument_type"),
        Index("ix_custom_strategies_created_at", "created_at"),
    )

    def to_dict(self):
        import json
        from datetime import datetime
        from decimal import Decimal
        d = {
            c.name: (
                float(v) if isinstance(v, Decimal) else
                v.isoformat() if isinstance(v, datetime) else
                json.loads(v) if c.name == "symbols" and v else
                bool(v) if c.name in ["auto_roll", "auto_adjust"] else
                v
            )
            for c, v in zip(self.__table__.columns, [
                getattr(self, c.name) for c in self.__table__.columns
            ], strict=False)
        }
        # Expose rules_json as a parsed object under "rules" — the API/UI
        # never needs to handle the raw JSON string.
        d["rules"] = json.loads(self.rules_json) if self.rules_json else None
        return d


class CustomStrategyPosition(Base):
    """
    One leg of an open (or closed) custom-strategy basket, opened by the
    paper/live scheduler (api/custom_strategy_scheduler.py). A strategy with
    N legs (see rule_schema.py) produces N rows per entry — this is the
    generalization of Position/EquityPosition for arbitrary multi-leg
    custom strategies, since those tables are shaped for the fixed
    options/equity screens and don't carry a strategy_id or leg concept.
    """
    __tablename__ = "custom_strategy_positions"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    strategy_id = Column(BigInteger, nullable=False)
    leg_index = Column(Integer, nullable=False)
    mode = Column(String(8), nullable=False)  # 'paper' | 'live'
    instrument_key = Column(String(128), nullable=False)
    instrument_type = Column(String(16), nullable=False)  # 'OPTION' | 'EQUITY' | 'FUTURE'
    option_type = Column(String(4), nullable=True)
    strike = Column(Numeric(12, 2), nullable=True)
    expiry = Column(String(10), nullable=True)
    transaction_type = Column(String(4), nullable=False)  # 'BUY' | 'SELL'
    quantity = Column(Integer, nullable=False)
    entry_price = Column(Numeric(12, 2), nullable=False)
    exit_price = Column(Numeric(12, 2), nullable=True)
    order_id = Column(String(64), nullable=True)
    exit_order_id = Column(String(64), nullable=True)
    status = Column(String(8), nullable=False, default="OPEN")  # 'OPEN' | 'CLOSED'
    exit_reason = Column(String(32), nullable=True)
    opened_at = Column(DateTime, nullable=False, server_default=func.now())
    closed_at = Column(DateTime, nullable=True)

    # This leg's resolved per-leg config (exit/trailing/sizing/expiry_mode
    # — see rule_schema.py), snapshotted from the strategy's rules_json AT
    # ENTRY TIME so a later rules edit never silently changes how an
    # already-open leg is managed. NULL for legs with no per-leg config
    # (managed by the strategy-level combined exit only, today's only
    # behavior) — see custom_strategy_scheduler.py::_try_exit.
    leg_config_json = Column(Text, nullable=True)
    # Live trailing-stop ratchet state for this leg (highest_price/
    # lowest_price/current_stop_price — see utils/trailing_stop.py). NULL
    # unless leg_config_json.exit.trailing.enabled is true.
    trail_state_json = Column(Text, nullable=True)

    __table_args__ = (
        Index("ix_custom_strategy_positions_strategy_id", "strategy_id"),
        Index("ix_custom_strategy_positions_status", "status"),
    )

    def to_dict(self):
        from datetime import datetime
        from decimal import Decimal
        return {
            c.name: (
                float(v) if isinstance(v, Decimal) else
                v.isoformat() if isinstance(v, datetime) else
                v
            )
            for c, v in zip(self.__table__.columns, [
                getattr(self, c.name) for c in self.__table__.columns
            ], strict=False)
        }


class Notification(Base):
    """
    In-app + Telegram alerting for background failures the user would
    otherwise never see — token/login failures, instrument master
    download failures, and custom-strategy execution failures (entry
    rejected, exit/square-off failed, auto-unwind failed) most likely
    because of insufficient margin/funds. See utils/notify.py, which is
    the only writer of this table — nothing should INSERT here directly.
    """
    __tablename__ = "notifications"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    level = Column(String(16), nullable=False, default="error")  # 'info' | 'warning' | 'error'
    source = Column(String(64), nullable=False)
    message = Column(Text, nullable=False)
    read = Column(Boolean, nullable=False, default=False)
    # Owning user (the CustomStrategy owner this notification is about), or
    # NULL for system-wide alerts with no single owner (broker login
    # failure, instrument master download failure) — those are only ever
    # shown to admins, see routes_notifications.py.
    user_id = Column(BigInteger, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    __table_args__ = (
        Index("ix_notifications_created_at", "created_at"),
        Index("ix_notifications_read", "read"),
        Index("ix_notifications_user_id", "user_id"),
    )

    def to_dict(self):
        return {
            "id": self.id,
            "level": self.level,
            "source": self.source,
            "message": self.message,
            "read": bool(self.read),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class CustomBacktestRun(Base):
    """
    One asynchronous CUSTOM STRATEGY backtest execution
    (api/routes_custom_strategies.py's POST /{id}/backtest kicks this off
    and returns immediately; the frontend polls GET
    /{id}/backtest/runs/{run_id} for progress/result). Gives backtests
    actual run history — CustomStrategy.backtest_result_json only ever
    held the single most recent run, with no way to compare two rule
    tweaks against each other. rules_snapshot_json freezes what the
    strategy's rules were AT RUN TIME, since a user can edit rules after a
    run completes and the stored result would otherwise silently drift out
    of sync with what's shown as "current rules" on the Strategies page.

    NOT the same table as the legacy BacktestRun above (backtest_runs) —
    that one is utils/backtest_history.py's flat single-cycle-count row
    for the old hand-written strategies (run_daemon.py-era); this is a
    richer JSON-result row scoped to the custom-strategy-builder's async
    backtest engine — hence the different name/table despite both being
    "a backtest run".
    """
    __tablename__ = "custom_backtest_runs"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    strategy_id = Column(BigInteger, nullable=False)
    user_id = Column(BigInteger, nullable=False)
    status = Column(String(16), nullable=False, default="QUEUED")  # 'QUEUED' | 'RUNNING' | 'COMPLETED' | 'FAILED'
    from_date = Column(String(10), nullable=True)
    to_date = Column(String(10), nullable=True)
    rules_snapshot_json = Column(Text, nullable=False)
    result_json = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)
    progress_current = Column(Integer, nullable=False, default=0)
    progress_total = Column(Integer, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    completed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_custom_backtest_runs_strategy_id", "strategy_id"),
        Index("ix_custom_backtest_runs_user_id", "user_id"),
        Index("ix_custom_backtest_runs_created_at", "created_at"),
    )

    def to_dict(self, include_result: bool = True):
        import json
        d = {
            "run_id": self.id,
            "strategy_id": self.strategy_id,
            "status": self.status,
            "from_date": self.from_date,
            "to_date": self.to_date,
            "progress_current": self.progress_current,
            "progress_total": self.progress_total,
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }
        if include_result:
            d["result"] = json.loads(self.result_json) if self.result_json else None
        return d


class SymbolIvHistory(Base):
    """
    One daily ATM-IV snapshot per symbol, written once/day by
    api/iv_history_scheduler.py after market close. The only source of
    IV-over-time data in this codebase — utils/black76.py computes IV
    live, on demand, per open leg, but nothing persisted it historically
    before this table, so an "IV rank" entry condition (rule_schema.py's
    entry.condition IV_RANK) had nothing to rank against. A symbol's rank
    is only meaningful once enough rows have accumulated (see
    utils/iv_rank.py — returns None, never a fabricated number, below
    that floor) — this table starts empty for every symbol and fills in
    day by day from whenever this feature first shipped, not backfilled
    from history that doesn't exist.
    """
    __tablename__ = "symbol_iv_history"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    symbol = Column(String(32), nullable=False)
    trade_date = Column(String(10), nullable=False)  # 'YYYY-MM-DD'
    atm_iv = Column(Numeric(8, 4), nullable=False)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    __table_args__ = (
        Index("ix_symbol_iv_history_symbol_date", "symbol", "trade_date"),
    )
