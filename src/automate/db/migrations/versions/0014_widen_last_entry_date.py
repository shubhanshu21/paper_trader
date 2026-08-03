"""widen custom_strategies.last_entry_date from VARCHAR(10) to TEXT

The column was sized for the original plain 'YYYY-MM-DD' format. This
session's cycle-aware entry tracking (custom_strategy_scheduler.py's
_get_last_entered_expiry/_set_last_entered_expiry) repurposed the same
column to store a per-symbol JSON blob (e.g.
{"RELIANCE": {"expiry": "2026-08-27", "date": "2026-07-31"}}), which is
routinely longer than 10 characters — every write since then has been
silently failing with MySQL error 1406 "Data too long for column", which
rolled back the WHOLE entry transaction (including the new
CustomStrategyPosition rows), and because the cycle-tracking flag never
persisted, the scheduler kept re-entering the same strategy on every
tick. Caught live: 2+ duplicate RELIANCE strangle entries placed with the
paper broker within minutes, none ever recorded in
custom_strategy_positions.

Revision ID: 0014
Revises: 0013
Create Date: 2026-07-31 07:05:00.000000

"""
import sqlalchemy as sa
from alembic import op

revision = '0014'
down_revision = '0013'
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column(
        'custom_strategies', 'last_entry_date',
        existing_type=sa.String(10),
        type_=sa.Text(),
        existing_nullable=True,
    )


def downgrade():
    op.alter_column(
        'custom_strategies', 'last_entry_date',
        existing_type=sa.Text(),
        type_=sa.String(10),
        existing_nullable=True,
    )
