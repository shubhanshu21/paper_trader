"""add index_1min_candles table

1-minute underlying (NIFTY/BANKNIFTY) OHLC candles, sourced from several
free Kaggle-hosted datasets — see scripts/import_kaggle_index_candles.py
for the ingestion script and db/models.py's Index1MinCandle for why this
is underlying-only (no option premiums) and deliberately separate from
fno_bhavcopy (daily, all F&O symbols, broker-sourced).

Revision ID: 0026
Revises: 0025
Create Date: 2026-08-07 19:40:00.000000

"""
import sqlalchemy as sa
from alembic import op

revision = '0026'
down_revision = '0025'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'index_1min_candles',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('symbol', sa.String(length=16), nullable=False),
        sa.Column('ts', sa.DateTime(), nullable=False),
        sa.Column('open', sa.Numeric(12, 4), nullable=False),
        sa.Column('high', sa.Numeric(12, 4), nullable=False),
        sa.Column('low', sa.Numeric(12, 4), nullable=False),
        sa.Column('close', sa.Numeric(12, 4), nullable=False),
        sa.Column('volume', sa.BigInteger(), nullable=True),
        sa.Column('oi', sa.BigInteger(), nullable=True),
        sa.Column('source', sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_index1min_symbol_ts', 'index_1min_candles', ['symbol', 'ts'], unique=True)


def downgrade():
    op.drop_index('ix_index1min_symbol_ts', table_name='index_1min_candles')
    op.drop_table('index_1min_candles')
