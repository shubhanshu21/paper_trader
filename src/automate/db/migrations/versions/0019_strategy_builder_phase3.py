"""per-leg config/trailing state + symbol_iv_history table

Backs the strategy-builder Phase 3 additions: per-leg exit/trailing-stop/
sizing/expiry-mode config (rule_schema.py), and the new IV-rank entry
condition's daily IV-history table. Purely additive — every new column
is nullable and every existing strategy's rules_json is untouched, so
this migration changes nothing about how any existing strategy runs.

Revision ID: 0019
Revises: 0018
Create Date: 2026-08-02 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = '0019'
down_revision = '0018'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('custom_strategy_positions', sa.Column('leg_config_json', sa.Text, nullable=True))
    op.add_column('custom_strategy_positions', sa.Column('trail_state_json', sa.Text, nullable=True))

    op.create_table(
        'symbol_iv_history',
        sa.Column('id', sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column('symbol', sa.String(32), nullable=False),
        sa.Column('trade_date', sa.String(10), nullable=False),
        sa.Column('atm_iv', sa.Numeric(8, 4), nullable=False),
        sa.Column('created_at', sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index('ix_symbol_iv_history_symbol_date', 'symbol_iv_history', ['symbol', 'trade_date'])


def downgrade():
    op.drop_index('ix_symbol_iv_history_symbol_date', table_name='symbol_iv_history')
    op.drop_table('symbol_iv_history')
    op.drop_column('custom_strategy_positions', 'trail_state_json')
    op.drop_column('custom_strategy_positions', 'leg_config_json')
