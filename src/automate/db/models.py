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
    BigInteger, Boolean, Column, DateTime, Index, Integer,
    Numeric, SmallInteger, String, Text, func,
)

from automate.db.engine import Base


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
            ])
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
# Historical dataset: intraday minute candles
# ---------------------------------------------------------------------------
class Candle(Base):
    __tablename__ = "candles"

    id           = Column(BigInteger, primary_key=True, autoincrement=True)
    symbol       = Column(String(32),  nullable=False)
    leg          = Column(String(8),   nullable=False)
    source_file  = Column(String(256), nullable=False)
    timestamp    = Column(String(26),  nullable=False)   # ISO datetime string
    open         = Column(Numeric(12, 4), nullable=True)
    high         = Column(Numeric(12, 4), nullable=True)
    low          = Column(Numeric(12, 4), nullable=True)
    close        = Column(Numeric(12, 4), nullable=True)
    volume       = Column(Numeric(18, 4), nullable=True)
    open_interest = Column(Numeric(18, 4), nullable=True)

    __table_args__ = (
        Index("ix_candles_symbol_leg_ts", "symbol", "leg", "timestamp"),
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


# ---------------------------------------------------------------------------
# Panel authentication: registered users
# ---------------------------------------------------------------------------
class User(Base):
    """
    Web panel user account.

    Security:
    - Passwords are never stored in plaintext — only bcrypt hashes.
    - role: 'admin' can create/manage users; 'viewer' is read-only.
    - is_active: deactivated accounts cannot log in (all sessions should be
      invalidated separately when deactivating — TODO(security): implement
      session revocation list if long-lived sessions are needed).
    """
    __tablename__ = "panel_users"

    id             = Column(BigInteger, primary_key=True, autoincrement=True)
    username       = Column(String(64),  nullable=False, unique=True)
    email          = Column(String(254), nullable=False, unique=True)
    hashed_password = Column(String(256), nullable=False)
    role           = Column(String(16),  nullable=False, default="viewer")   # 'admin' | 'viewer'
    is_active      = Column(Integer,     nullable=False, default=1)           # 0 = deactivated
    created_at     = Column(DateTime,    nullable=False, server_default=func.now())

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
            ])
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
            ])
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
# Order execution tracking
# ---------------------------------------------------------------------------
class OrderExecution(Base):
    """
    Real-time order execution tracking from broker.
    Stores order status updates from broker for monitoring and reconciliation.
    """
    __tablename__ = "order_executions"

    id               = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id          = Column(BigInteger, nullable=True)  # Optional: track which user placed order
    order_id         = Column(String(64), nullable=False, unique=True)  # Broker order ID
    instrument_key   = Column(String(64), nullable=False)
    direction        = Column(String(8), nullable=False)  # BUY | SELL
    quantity         = Column(Integer, nullable=False)
    price            = Column(Numeric(12, 4), nullable=True)  # Limit price or filled price
    product          = Column(String(8), nullable=False)  # CNC | MIS | NRML
    mode             = Column(String(8), nullable=False)  # paper | live
    status           = Column(String(16), nullable=False)  # PENDING | OPEN | COMPLETE | REJECTED | CANCELLED
    status_message   = Column(String(256), nullable=True)
    filled_quantity  = Column(Integer, nullable=True)
    filled_price     = Column(Numeric(12, 4), nullable=True)
    created_at       = Column(DateTime, nullable=False, server_default=func.now())
    updated_at       = Column(DateTime, nullable=True, onupdate=func.now())
    strategy_name    = Column(String(128), nullable=True)  # Strategy that generated order, if any

    __table_args__ = (
        Index("ix_order_executions_user_id", "user_id"),
        Index("ix_order_executions_order_id", "order_id"),
        Index("ix_order_executions_status", "status"),
        Index("ix_order_executions_instrument", "instrument_key"),
        Index("ix_order_executions_created_at", "created_at"),
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
            ])
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
            ])
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
            ])
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
