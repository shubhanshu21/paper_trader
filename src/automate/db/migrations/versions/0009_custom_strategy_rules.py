"""add rules_json/last_entry_date to custom_strategies + custom_strategy_positions table

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-30 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import BigInteger, Column, DateTime, Integer, Numeric, String, Text, func


# revision identifiers, used by Alembic.
revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('custom_strategies', sa.Column('rules_json', Text(), nullable=True))
    op.add_column('custom_strategies', sa.Column('last_entry_date', String(10), nullable=True))

    op.create_table(
        'custom_strategy_positions',
        Column('id', BigInteger, primary_key=True, autoincrement=True),
        Column('strategy_id', BigInteger, nullable=False),
        Column('leg_index', Integer, nullable=False),
        Column('mode', String(8), nullable=False),  # 'paper' | 'live'
        Column('instrument_key', String(128), nullable=False),
        Column('instrument_type', String(16), nullable=False),  # 'OPTION' | 'EQUITY' | 'FUTURE'
        Column('option_type', String(4), nullable=True),  # 'CE' | 'PE' | None
        Column('strike', Numeric(12, 2), nullable=True),
        Column('expiry', String(10), nullable=True),
        Column('transaction_type', String(4), nullable=False),  # 'BUY' | 'SELL'
        Column('quantity', Integer, nullable=False),
        Column('entry_price', Numeric(12, 2), nullable=False),
        Column('exit_price', Numeric(12, 2), nullable=True),
        Column('order_id', String(64), nullable=True),
        Column('exit_order_id', String(64), nullable=True),
        Column('status', String(8), nullable=False, server_default='OPEN'),  # 'OPEN' | 'CLOSED'
        Column('exit_reason', String(32), nullable=True),
        Column('opened_at', DateTime, nullable=False, server_default=func.now()),
        Column('closed_at', DateTime, nullable=True),
    )
    op.create_index('ix_custom_strategy_positions_strategy_id', 'custom_strategy_positions', ['strategy_id'])
    op.create_index('ix_custom_strategy_positions_status', 'custom_strategy_positions', ['status'])


def downgrade():
    op.drop_index('ix_custom_strategy_positions_status', table_name='custom_strategy_positions')
    op.drop_index('ix_custom_strategy_positions_strategy_id', table_name='custom_strategy_positions')
    op.drop_table('custom_strategy_positions')
    op.drop_column('custom_strategies', 'last_entry_date')
    op.drop_column('custom_strategies', 'rules_json')
