"""add max_daily_drawdown_pct to wallet_settings

Lets a user opt into an automatic global kill-switch trip once today's
realized LIVE P&L drops past a configured % of starting capital —
NULL (the default) means no auto-trigger, matching this table's existing
"NULL = use the default / opt out" convention for every other per-user
override column.

Revision ID: 0025
Revises: 0024
Create Date: 2026-08-07 14:20:00.000000

"""
import sqlalchemy as sa
from alembic import op

revision = '0025'
down_revision = '0024'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('wallet_settings', sa.Column('max_daily_drawdown_pct', sa.Numeric(6, 2), nullable=True))


def downgrade():
    op.drop_column('wallet_settings', 'max_daily_drawdown_pct')
