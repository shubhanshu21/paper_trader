"""add advanced_orders table

routes_advanced_orders.py's OCO/trailing-stop/bracket order endpoints used
to be pure in-memory dicts — no real broker order was ever placed, nothing
auto-cancelled an OCO sibling or advanced a trailing stop, and all state
vanished on process restart. This table backs those endpoints for real,
driven each tick by api/advanced_orders_scheduler.py.

Revision ID: 0017
Revises: 0016
Create Date: 2026-08-01 00:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

revision = '0017'
down_revision = '0016'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'advanced_orders',
        sa.Column('id', sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column('public_id', sa.String(40), nullable=False, unique=True),
        sa.Column('kind', sa.String(16), nullable=False),
        sa.Column('user_id', sa.BigInteger, nullable=False),
        sa.Column('mode', sa.String(8), nullable=False),
        sa.Column('strategy_name', sa.String(64), nullable=True),
        sa.Column('status', sa.String(16), nullable=False, server_default='ACTIVE'),
        sa.Column('state_json', sa.Text, nullable=False),
        sa.Column('created_at', sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime, nullable=True),
    )
    op.create_index('ix_advanced_orders_public_id', 'advanced_orders', ['public_id'])
    op.create_index('ix_advanced_orders_user_id', 'advanced_orders', ['user_id'])
    op.create_index('ix_advanced_orders_status', 'advanced_orders', ['status'])


def downgrade():
    op.drop_index('ix_advanced_orders_status', table_name='advanced_orders')
    op.drop_index('ix_advanced_orders_user_id', table_name='advanced_orders')
    op.drop_index('ix_advanced_orders_public_id', table_name='advanced_orders')
    op.drop_table('advanced_orders')
