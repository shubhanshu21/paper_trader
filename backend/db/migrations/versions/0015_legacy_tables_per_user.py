"""per-user isolation for the legacy Position/EquityPosition/WalletSettings
tables — these predate the Custom Strategy Builder's ownership model
(migration 0013) and had zero user_id concept, so every logged-in account
shared the same positions and the same single wallet. Both `positions`
and `equity_positions` are currently empty (confirmed before writing this
migration), so this is a schema-only change with no backfill needed there.
`wallet_settings` has exactly one existing row (id=1) which is backfilled
to the sole existing admin account, same approach as migration 0013 took
for orphan custom_strategies rows.

Revision ID: 0015
Revises: 0014
Create Date: 2026-07-31 09:10:00.000000

"""
import sqlalchemy as sa
from alembic import op

revision = '0015'
down_revision = '0014'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('positions', sa.Column('user_id', sa.BigInteger(), nullable=True))
    op.create_index('ix_positions_user_id', 'positions', ['user_id'])

    op.add_column('equity_positions', sa.Column('user_id', sa.BigInteger(), nullable=True))
    op.create_index('ix_equity_positions_user_id', 'equity_positions', ['user_id'])

    op.add_column('wallet_settings', sa.Column('user_id', sa.BigInteger(), nullable=True))
    op.create_index('ix_wallet_settings_user_id', 'wallet_settings', ['user_id'], unique=True)

    conn = op.get_bind()
    admin_id = conn.execute(sa.text(
        "SELECT id FROM panel_users WHERE role = 'admin' ORDER BY id ASC LIMIT 1"
    )).scalar()
    if admin_id is not None:
        conn.execute(sa.text(
            "UPDATE wallet_settings SET user_id = :admin_id WHERE user_id IS NULL"
        ), {"admin_id": admin_id})


def downgrade():
    op.drop_index('ix_wallet_settings_user_id', table_name='wallet_settings')
    op.drop_column('wallet_settings', 'user_id')

    op.drop_index('ix_equity_positions_user_id', table_name='equity_positions')
    op.drop_column('equity_positions', 'user_id')

    op.drop_index('ix_positions_user_id', table_name='positions')
    op.drop_column('positions', 'user_id')
