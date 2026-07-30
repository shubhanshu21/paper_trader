"""per-user data isolation: notifications.user_id + backfill orphan
custom_strategies.user_id

Custom strategies/positions/notifications were never actually scoped to
the logged-in panel user (see db.models.CustomStrategy.user_id, which
existed but was optional and never enforced by any endpoint) — every
account saw every strategy. This adds the missing user_id column to
notifications (nullable: system-wide alerts like broker-login/instrument-
download failures aren't owned by any one user and stay NULL, visible to
admins only) and backfills the one pre-existing orphan custom_strategies
row (created before ownership was enforced) to the sole admin account at
the time, so that account doesn't lose visibility into its own work.

Revision ID: 0013
Revises: 0012
Create Date: 2026-07-30 19:15:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = '0013'
down_revision = '0012'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('notifications', sa.Column('user_id', sa.BigInteger(), nullable=True))
    op.create_index('ix_notifications_user_id', 'notifications', ['user_id'])

    # Backfill: assign any strategy created before ownership enforcement
    # (user_id IS NULL) to the earliest-created admin account, so it
    # doesn't silently vanish from that account's strategy list once
    # every list/read endpoint starts filtering by user_id.
    conn = op.get_bind()
    admin_id = conn.execute(sa.text(
        "SELECT id FROM panel_users WHERE role = 'admin' ORDER BY id ASC LIMIT 1"
    )).scalar()
    if admin_id is not None:
        conn.execute(sa.text(
            "UPDATE custom_strategies SET user_id = :admin_id WHERE user_id IS NULL"
        ), {"admin_id": admin_id})


def downgrade():
    op.drop_index('ix_notifications_user_id', table_name='notifications')
    op.drop_column('notifications', 'user_id')
