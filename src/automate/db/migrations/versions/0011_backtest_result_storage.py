"""add backtest_result_json to custom_strategies — persists full backtest
results (per-cycle detail) so they can be viewed anytime, not just right
after running; overwritten on every re-run for the same strategy.

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-30 19:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('custom_strategies', sa.Column('backtest_result_json', sa.Text(), nullable=True))
    op.add_column('custom_strategies', sa.Column('backtest_run_at', sa.DateTime(), nullable=True))


def downgrade():
    op.drop_column('custom_strategies', 'backtest_run_at')
    op.drop_column('custom_strategies', 'backtest_result_json')
