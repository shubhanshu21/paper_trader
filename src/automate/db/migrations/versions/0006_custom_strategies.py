"""add custom_strategies table

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-29 20:25:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import BigInteger, DateTime, Integer, Numeric, SmallInteger, String, Text, func


# revision identifiers, used by Alembic.
revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'custom_strategies',
        sa.Column('id', BigInteger(), autoincrement=True, nullable=False),
        sa.Column('user_id', BigInteger(), nullable=True),
        sa.Column('name', String(128), nullable=False),
        sa.Column('description', Text(), nullable=True),
        sa.Column('instrument_type', String(16), nullable=False),
        sa.Column('symbols', Text(), nullable=False),
        sa.Column('strategy_type', String(32), nullable=False),
        sa.Column('option_type', String(8), nullable=False),
        sa.Column('strike_offset', Numeric(8, 4), nullable=True),
        sa.Column('expiry_days', Integer(), nullable=True),
        sa.Column('num_lots', Integer(), nullable=False, server_default='1'),
        sa.Column('take_profit_pct', Numeric(8, 4), nullable=True),
        sa.Column('stop_loss_pct', Numeric(8, 4), nullable=True),
        sa.Column('exit_days_before_expiry', Integer(), nullable=False, server_default='1'),
        sa.Column('status', String(16), nullable=False, server_default='DRAFT'),
        sa.Column('backtest_return_pct', Numeric(8, 4), nullable=True),
        sa.Column('paper_return_pct', Numeric(8, 4), nullable=True),
        sa.Column('live_return_pct', Numeric(8, 4), nullable=True),
        sa.Column('created_at', DateTime(), nullable=False, server_default=func.now()),
        sa.Column('updated_at', DateTime(), nullable=True, onupdate=func.now()),
        sa.Column('deployed_at', DateTime(), nullable=True),
        sa.Column('auto_roll', SmallInteger(), nullable=False, server_default='0'),
        sa.Column('roll_threshold_pct', Numeric(8, 4), nullable=True),
        sa.Column('auto_adjust', SmallInteger(), nullable=False, server_default='0'),
        sa.Column('greek_threshold_delta', Numeric(8, 4), nullable=True),
        sa.Column('greek_threshold_theta', Numeric(8, 4), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.Index('ix_custom_strategies_user_id', 'user_id'),
        sa.Index('ix_custom_strategies_status', 'status'),
        sa.Index('ix_custom_strategies_instrument_type', 'instrument_type'),
        sa.Index('ix_custom_strategies_created_at', 'created_at'),
    )


def downgrade():
    op.drop_table('custom_strategies')
