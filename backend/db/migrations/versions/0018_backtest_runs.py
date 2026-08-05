"""add custom_backtest_runs table

routes_custom_strategies.py's POST /{id}/backtest used to run fully
synchronously in the HTTP request and keep only one stored result per
strategy (CustomStrategy.backtest_result_json, overwritten every run) —
no run history, no way to compare two rule tweaks, and a real timeout risk
on long histories. This table backs async execution + run history for the
custom-strategy-builder's backtest engine specifically — NOT the same as
the pre-existing legacy `backtest_runs` table (db/models.py's BacktestRun,
utils/backtest_history.py, the old hand-written-strategy era), hence the
`custom_` prefix rather than colliding with it.

Revision ID: 0018
Revises: 0017
Create Date: 2026-08-02 00:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

revision = '0018'
down_revision = '0017'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'custom_backtest_runs',
        sa.Column('id', sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column('strategy_id', sa.BigInteger, nullable=False),
        sa.Column('user_id', sa.BigInteger, nullable=False),
        sa.Column('status', sa.String(16), nullable=False, server_default='QUEUED'),
        sa.Column('from_date', sa.String(10), nullable=True),
        sa.Column('to_date', sa.String(10), nullable=True),
        sa.Column('rules_snapshot_json', sa.Text, nullable=False),
        sa.Column('result_json', sa.Text, nullable=True),
        sa.Column('error_message', sa.Text, nullable=True),
        sa.Column('progress_current', sa.Integer, nullable=False, server_default='0'),
        sa.Column('progress_total', sa.Integer, nullable=True),
        sa.Column('created_at', sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column('completed_at', sa.DateTime, nullable=True),
    )
    op.create_index('ix_custom_backtest_runs_strategy_id', 'custom_backtest_runs', ['strategy_id'])
    op.create_index('ix_custom_backtest_runs_user_id', 'custom_backtest_runs', ['user_id'])
    op.create_index('ix_custom_backtest_runs_created_at', 'custom_backtest_runs', ['created_at'])


def downgrade():
    op.drop_index('ix_custom_backtest_runs_created_at', table_name='custom_backtest_runs')
    op.drop_index('ix_custom_backtest_runs_user_id', table_name='custom_backtest_runs')
    op.drop_index('ix_custom_backtest_runs_strategy_id', table_name='custom_backtest_runs')
    op.drop_table('custom_backtest_runs')
