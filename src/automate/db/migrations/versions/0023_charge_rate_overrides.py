"""add per-user F&O charge rate overrides to wallet_settings

Lets a user override any component of the transaction-cost formula
(utils/costs.py's DEFAULT_RATES) from the Profile page — brokerage per
order, exchange transaction charge %, GST %, STT %, SEBI charge %, stamp
duty % — so the app can be kept in sync with NSE/SEBI/govt rate changes
without a code deploy. All columns are nullable: NULL means "use the
codebase default for that component".

Revision ID: 0023
Revises: 0022
Create Date: 2026-08-03 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = '0023'
down_revision = '0022'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('wallet_settings', sa.Column('brokerage_per_order', sa.Numeric(10, 2), nullable=True))
    op.add_column('wallet_settings', sa.Column('exchange_charge_pct', sa.Numeric(12, 8), nullable=True))
    op.add_column('wallet_settings', sa.Column('gst_pct', sa.Numeric(6, 4), nullable=True))
    op.add_column('wallet_settings', sa.Column('stt_pct', sa.Numeric(6, 4), nullable=True))
    op.add_column('wallet_settings', sa.Column('sebi_charge_pct', sa.Numeric(12, 8), nullable=True))
    op.add_column('wallet_settings', sa.Column('stamp_duty_pct', sa.Numeric(12, 8), nullable=True))


def downgrade():
    op.drop_column('wallet_settings', 'stamp_duty_pct')
    op.drop_column('wallet_settings', 'sebi_charge_pct')
    op.drop_column('wallet_settings', 'stt_pct')
    op.drop_column('wallet_settings', 'gst_pct')
    op.drop_column('wallet_settings', 'exchange_charge_pct')
    op.drop_column('wallet_settings', 'brokerage_per_order')
