"""add custom_strategies.pre_pause_status

routes_strategy_deployment.py's resume_strategy() previously hardcoded the
resumed status to PAPER_TRADING regardless of what the strategy was
actually running as before it was paused — a LIVE strategy would be
silently downgraded to paper trading on resume while reporting success.
This column lets pause_strategy() record the mode it was in, and
resume_strategy() restore it.

Revision ID: 0016
Revises: 0015
Create Date: 2026-08-01 00:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

revision = '0016'
down_revision = '0015'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'custom_strategies',
        sa.Column('pre_pause_status', sa.String(16), nullable=True),
    )


def downgrade():
    op.drop_column('custom_strategies', 'pre_pause_status')
