"""Initial schema — creates all 4 production tables.

Revision: 0001
"""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── positions ─────────────────────────────────────────────────────────────
    op.create_table(
        "positions",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("strategy_name",    sa.String(128), nullable=False),
        sa.Column("mode",             sa.String(8),   nullable=False, server_default="paper"),
        sa.Column("symbol",           sa.String(32),  nullable=False),
        sa.Column("entry_date",       sa.String(10),  nullable=False),
        sa.Column("expiry",           sa.String(10),  nullable=False),
        sa.Column("call_token",       sa.String(256), nullable=False),
        sa.Column("call_strike",      sa.Integer,     nullable=False),
        sa.Column("call_entry_price", sa.Numeric(12, 4), nullable=False),
        sa.Column("call_order_id",    sa.String(64),  nullable=True),
        sa.Column("put_token",        sa.String(256), nullable=False),
        sa.Column("put_strike",       sa.Integer,     nullable=False),
        sa.Column("put_entry_price",  sa.Numeric(12, 4), nullable=False),
        sa.Column("put_order_id",     sa.String(64),  nullable=True),
        sa.Column("quantity",         sa.Integer,     nullable=False),
        sa.Column("product",          sa.String(8),   nullable=False),
        sa.Column("take_profit_pct",  sa.Numeric(8, 4),  nullable=True),
        sa.Column("stop_loss_pct",    sa.Numeric(8, 4),  nullable=True),
        sa.Column("exit_days_before_expiry", sa.SmallInteger, nullable=False, server_default="1"),
        sa.Column("status",           sa.String(8),   nullable=False, server_default="OPEN"),
        sa.Column("exit_date",        sa.String(10),  nullable=True),
        sa.Column("exit_reason",      sa.String(32),  nullable=True),
        sa.Column("call_exit_price",  sa.Numeric(12, 4), nullable=True),
        sa.Column("put_exit_price",   sa.Numeric(12, 4), nullable=True),
        sa.Column("call_exit_order_id", sa.String(64), nullable=True),
        sa.Column("put_exit_order_id",  sa.String(64), nullable=True),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )
    op.create_index("ix_positions_status",                "positions", ["status"])
    op.create_index("ix_positions_strategy_symbol_status","positions", ["strategy_name", "symbol", "status"])
    op.create_index("ix_positions_mode_status",           "positions", ["mode", "status"])

    # ── backtest_runs ─────────────────────────────────────────────────────────
    op.create_table(
        "backtest_runs",
        sa.Column("id",              sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("run_at",          sa.String(26),   nullable=False),
        sa.Column("strategy_name",   sa.String(128),  nullable=False),
        sa.Column("symbol",          sa.String(32),   nullable=False),
        sa.Column("contract_type",   sa.String(16),   nullable=False),
        sa.Column("from_date",       sa.String(10),   nullable=False),
        sa.Column("to_date",         sa.String(10),   nullable=False),
        sa.Column("cycles",          sa.Integer,      nullable=False),
        sa.Column("wins",            sa.Integer,      nullable=False),
        sa.Column("win_rate_pct",    sa.Numeric(6, 2),  nullable=True),
        sa.Column("total_pnl",       sa.Numeric(16, 4), nullable=False),
        sa.Column("total_return_pct",sa.Numeric(10, 4), nullable=True),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )
    op.create_index("ix_backtest_runs_strategy_symbol", "backtest_runs", ["strategy_name", "symbol"])
    op.create_index("ix_backtest_runs_run_at",          "backtest_runs", ["run_at"])

    # ── fno_bhavcopy ──────────────────────────────────────────────────────────
    # NOTE: No foreign keys — this is a denormalized append-only dataset.
    # Indexes are created AFTER data load for performance (see migration script).
    op.create_table(
        "fno_bhavcopy",
        sa.Column("id",          sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("instrument",  sa.String(8),   nullable=False),
        sa.Column("symbol",      sa.String(32),  nullable=False),
        sa.Column("expiry_dt",   sa.String(10),  nullable=True),
        sa.Column("strike_pr",   sa.Numeric(12, 2), nullable=True),
        sa.Column("option_typ",  sa.String(2),   nullable=True),
        sa.Column("open",        sa.Numeric(12, 4), nullable=True),
        sa.Column("high",        sa.Numeric(12, 4), nullable=True),
        sa.Column("low",         sa.Numeric(12, 4), nullable=True),
        sa.Column("close",       sa.Numeric(12, 4), nullable=True),
        sa.Column("settle_pr",   sa.Numeric(12, 4), nullable=True),
        sa.Column("contracts",   sa.Integer,     nullable=True),
        sa.Column("val_inlakh",  sa.Numeric(16, 4), nullable=True),
        sa.Column("open_int",    sa.Integer,     nullable=True),
        sa.Column("chg_in_oi",   sa.Integer,     nullable=True),
        sa.Column("trade_date",  sa.String(10),  nullable=False),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
        mysql_row_format="COMPRESSED",  # ~30-40% size saving on text-heavy rows
    )

    # ── candles ───────────────────────────────────────────────────────────────
    op.create_table(
        "candles",
        sa.Column("id",            sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("symbol",        sa.String(32),  nullable=False),
        sa.Column("leg",           sa.String(8),   nullable=False),
        sa.Column("source_file",   sa.String(256), nullable=False),
        sa.Column("timestamp",     sa.String(26),  nullable=False),
        sa.Column("open",          sa.Numeric(12, 4), nullable=True),
        sa.Column("high",          sa.Numeric(12, 4), nullable=True),
        sa.Column("low",           sa.Numeric(12, 4), nullable=True),
        sa.Column("close",         sa.Numeric(12, 4), nullable=True),
        sa.Column("volume",        sa.Numeric(18, 4), nullable=True),
        sa.Column("open_interest", sa.Numeric(18, 4), nullable=True),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )
    op.create_index("ix_candles_symbol_leg_ts", "candles", ["symbol", "leg", "timestamp"])


def downgrade() -> None:
    op.drop_table("candles")
    op.drop_index("ix_bhav_symbol_instrument_expiry_date", table_name="fno_bhavcopy")
    op.drop_index("ix_bhav_symbol_instrument_expiry",      table_name="fno_bhavcopy")
    op.drop_index("ix_bhav_symbol_instrument_date",        table_name="fno_bhavcopy")
    op.drop_table("fno_bhavcopy")
    op.drop_table("backtest_runs")
    op.drop_index("ix_positions_mode_status",                table_name="positions")
    op.drop_index("ix_positions_strategy_symbol_status",     table_name="positions")
    op.drop_index("ix_positions_status",                     table_name="positions")
    op.drop_table("positions")
