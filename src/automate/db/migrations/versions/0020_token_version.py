"""add panel_users.token_version

Enables real session revocation — previously a JWT was valid until its
'exp' claim regardless of anything happening to the account afterward
(password change, deactivation, "log out everywhere"). Every issued
token now embeds the user's token_version as a 'tv' claim; bumping this
column (api/auth.py::bump_token_version) immediately invalidates every
token issued before the bump, checked on every authenticated request.

Revision ID: 0020
Revises: 0019
Create Date: 2026-08-02 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = '0020'
down_revision = '0019'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'panel_users',
        sa.Column('token_version', sa.Integer(), nullable=False, server_default='0'),
    )


def downgrade():
    op.drop_column('panel_users', 'token_version')
