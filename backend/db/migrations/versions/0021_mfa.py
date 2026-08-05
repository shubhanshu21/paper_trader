"""add panel_users MFA columns (TOTP + backup codes)

Revision ID: 0021
Revises: 0020
Create Date: 2026-08-02 00:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

revision = '0021'
down_revision = '0020'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('panel_users', sa.Column('mfa_enabled', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('panel_users', sa.Column('mfa_secret', sa.String(64), nullable=True))
    op.add_column('panel_users', sa.Column('mfa_backup_codes_json', sa.Text(), nullable=True))


def downgrade():
    op.drop_column('panel_users', 'mfa_backup_codes_json')
    op.drop_column('panel_users', 'mfa_secret')
    op.drop_column('panel_users', 'mfa_enabled')
