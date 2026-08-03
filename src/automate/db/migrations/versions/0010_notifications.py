"""add notifications table — in-app + Telegram alerting for background failures

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-30 14:00:00.000000

"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy import BigInteger, Boolean, Column, DateTime, String, Text, func

revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'notifications',
        Column('id', BigInteger, primary_key=True, autoincrement=True),
        Column('level', String(16), nullable=False, server_default='error'),  # 'info' | 'warning' | 'error'
        Column('source', String(64), nullable=False),
        Column('message', Text, nullable=False),
        Column('read', Boolean, nullable=False, server_default=sa.text('0')),
        Column('created_at', DateTime, nullable=False, server_default=func.now()),
    )
    op.create_index('ix_notifications_created_at', 'notifications', ['created_at'])
    op.create_index('ix_notifications_read', 'notifications', ['read'])


def downgrade():
    op.drop_index('ix_notifications_read', table_name='notifications')
    op.drop_index('ix_notifications_created_at', table_name='notifications')
    op.drop_table('notifications')
