"""add broker_tokens table — moves the Upstox access token out of .env

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-30 09:20:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import DateTime, String, Text, func


# revision identifiers, used by Alembic.
revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'broker_tokens',
        sa.Column('broker', String(32), nullable=False),
        sa.Column('access_token', Text(), nullable=True),
        sa.Column('updated_at', DateTime(), nullable=False, server_default=func.now(), onupdate=func.now()),
        sa.PrimaryKeyConstraint('broker'),
    )


def downgrade():
    op.drop_table('broker_tokens')
