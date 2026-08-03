"""Wallet settings — single-row table for the paper-trading starting capital.

Previously an env var (PAPER_STARTING_CAPITAL) with a hardcoded ₹10L
default — moved into the DB so it's a real, runtime-editable value (via
/api/wallet/capital) instead of something that needs an env edit + process
restart to change.

Revision: 0002
"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "wallet_settings",
        sa.Column("id", sa.SmallInteger, primary_key=True),
        sa.Column("starting_capital", sa.Numeric(16, 2), nullable=False, server_default="0"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )
    op.execute("INSERT INTO wallet_settings (id, starting_capital) VALUES (1, 0)")


def downgrade() -> None:
    op.drop_table("wallet_settings")
