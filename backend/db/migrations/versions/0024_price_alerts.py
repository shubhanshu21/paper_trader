"""add price_alerts table

New feature: "notify me when NIFTY crosses X" style price/condition
alerts, evaluated live by api/price_alert_scheduler.py against real LTP.
Independent of CustomStrategy — a plain watch-and-notify, never places
an order.

Revision ID: 0024
Revises: 0023
Create Date: 2026-08-07 13:30:00.000000

"""
import sqlalchemy as sa
from alembic import op

revision = '0024'
down_revision = '0023'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'price_alerts',
        sa.Column('id', sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.BigInteger, nullable=False),
        sa.Column('symbol', sa.String(32), nullable=False),
        sa.Column('condition', sa.String(16), nullable=False),
        sa.Column('target_price', sa.Numeric(14, 4), nullable=False),
        sa.Column('note', sa.String(255), nullable=True),
        sa.Column('status', sa.String(12), nullable=False, server_default='ACTIVE'),
        sa.Column('last_seen_price', sa.Numeric(14, 4), nullable=True),
        sa.Column('created_at', sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column('triggered_at', sa.DateTime, nullable=True),
        sa.Column('triggered_price', sa.Numeric(14, 4), nullable=True),
    )
    op.create_index('ix_price_alerts_user_id', 'price_alerts', ['user_id'])
    op.create_index('ix_price_alerts_status', 'price_alerts', ['status'])


def downgrade():
    op.drop_index('ix_price_alerts_status', table_name='price_alerts')
    op.drop_index('ix_price_alerts_user_id', table_name='price_alerts')
    op.drop_table('price_alerts')
