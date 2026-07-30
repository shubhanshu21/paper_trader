"""Create instruments table.

instruments: stores local copy of NSE spot/derivatives, BSE spot/derivatives,
and MCX commodity contract tokens, updated daily via InstrumentCache.

Revision: 0004
"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "instruments",
        sa.Column("instrument_key",  sa.String(64),     primary_key=True),
        sa.Column("exchange_token",  sa.String(64),     nullable=True),
        sa.Column("symbol",          sa.String(64),     nullable=False),
        sa.Column("name",            sa.String(128),    nullable=True),
        sa.Column("last_price",      sa.Numeric(12, 4), nullable=True),
        sa.Column("expiry",          sa.String(10),     nullable=True),
        sa.Column("strike",          sa.Numeric(12, 2), nullable=True),
        sa.Column("tick_size",       sa.Numeric(12, 4), nullable=True),
        sa.Column("lot_size",        sa.Integer,        nullable=True),
        sa.Column("instrument_type", sa.String(16),     nullable=True),
        sa.Column("option_type",     sa.String(16),     nullable=True),
        sa.Column("exchange",        sa.String(16),     nullable=False),

        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )
    op.create_index("ix_instruments_symbol",   "instruments", ["symbol"])
    op.create_index("ix_instruments_exchange", "instruments", ["exchange"])


def downgrade() -> None:
    op.drop_index("ix_instruments_exchange", table_name="instruments")
    op.drop_index("ix_instruments_symbol",   table_name="instruments")
    op.drop_table("instruments")
