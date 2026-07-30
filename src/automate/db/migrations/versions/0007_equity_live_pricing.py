"""add current_price/unrealized_pnl to equity_positions for live MTM

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-30 09:15:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import Numeric


# revision identifiers, used by Alembic.
revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('equity_positions', sa.Column('current_price', Numeric(12, 4), nullable=True))
    op.add_column('equity_positions', sa.Column('unrealized_pnl', Numeric(16, 4), nullable=True))
    op.add_column('equity_positions', sa.Column('price_updated_at', sa.DateTime(), nullable=True))


def downgrade():
    op.drop_column('equity_positions', 'price_updated_at')
    op.drop_column('equity_positions', 'unrealized_pnl')
    op.drop_column('equity_positions', 'current_price')
