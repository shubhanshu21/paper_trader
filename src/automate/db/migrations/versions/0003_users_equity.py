"""Create panel_users and equity_positions tables.

panel_users: stores web control panel user accounts for the optional
auth gate (PANEL_AUTH_ENABLED=true). Passwords stored as bcrypt hashes.

equity_positions: stores equity (spot/CNC or intraday/MIS) positions
opened by equity strategies (e.g. equity_ma_crossover). Separate from
the options-only `positions` table.

Revision: 0003
"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Panel users (auth) ───────────────────────────────────────────────────
    op.create_table(
        "panel_users",
        sa.Column("id",              sa.BigInteger,    primary_key=True, autoincrement=True),
        sa.Column("username",        sa.String(64),    nullable=False),
        sa.Column("email",           sa.String(254),   nullable=False),
        sa.Column("hashed_password", sa.String(256),   nullable=False),
        sa.Column("role",            sa.String(16),    nullable=False, server_default="viewer"),
        sa.Column("is_active",       sa.Integer,       nullable=False, server_default="1"),
        sa.Column(
            "created_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint("username", name="uq_panel_users_username"),
        sa.UniqueConstraint("email",    name="uq_panel_users_email"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )
    op.create_index("ix_panel_users_username", "panel_users", ["username"])
    op.create_index("ix_panel_users_email",    "panel_users", ["email"])

    # ── Equity positions ─────────────────────────────────────────────────────
    op.create_table(
        "equity_positions",
        sa.Column("id",             sa.BigInteger,     primary_key=True, autoincrement=True),
        sa.Column("strategy_name",  sa.String(128),    nullable=False),
        sa.Column("mode",           sa.String(8),      nullable=False, server_default="paper"),
        sa.Column("symbol",         sa.String(32),     nullable=False),
        sa.Column("direction",      sa.String(8),      nullable=False, server_default="LONG"),
        sa.Column("product",        sa.String(8),      nullable=False, server_default="CNC"),
        sa.Column("entry_date",     sa.String(10),     nullable=False),
        sa.Column("entry_price",    sa.Numeric(12, 4), nullable=False),
        sa.Column("quantity",       sa.Integer,        nullable=False),
        sa.Column("entry_order_id", sa.String(64),     nullable=True),
        sa.Column("status",         sa.String(8),      nullable=False, server_default="OPEN"),
        sa.Column("exit_date",      sa.String(10),     nullable=True),
        sa.Column("exit_price",     sa.Numeric(12, 4), nullable=True),
        sa.Column("exit_reason",    sa.String(64),     nullable=True),
        sa.Column("exit_order_id",  sa.String(64),     nullable=True),
        sa.Column("gross_pnl",      sa.Numeric(16, 4), nullable=True),
        sa.Column("net_pnl",        sa.Numeric(16, 4), nullable=True),
        sa.Column("charges",        sa.Numeric(12, 4), nullable=True),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )
    op.create_index("ix_equity_positions_status",          "equity_positions", ["status"])
    op.create_index("ix_equity_positions_strategy_symbol", "equity_positions", ["strategy_name", "symbol", "status"])
    op.create_index("ix_equity_positions_mode_status",     "equity_positions", ["mode", "status"])


def downgrade() -> None:
    op.drop_index("ix_equity_positions_mode_status",     table_name="equity_positions")
    op.drop_index("ix_equity_positions_strategy_symbol", table_name="equity_positions")
    op.drop_index("ix_equity_positions_status",          table_name="equity_positions")
    op.drop_table("equity_positions")

    op.drop_index("ix_panel_users_email",    table_name="panel_users")
    op.drop_index("ix_panel_users_username", table_name="panel_users")
    op.drop_table("panel_users")
