"""add the fno_bhavcopy indexes migration 0001 deferred ("created AFTER
data load for performance") — never actually applied to this DB. Without
them, every backtest query does a full scan of a 170M+ row table, which
was observed live holding a MySQL metadata lock for 100+ seconds and
blocking ALL unrelated queries against custom_strategies for that whole
time (any table's ALTER/long transaction can be queued behind another
table's slow query within the same session/transaction).

ALGORITHM=INPLACE, LOCK=NONE lets reads/writes continue concurrently
while each index builds (only brief metadata locks at start/end) — still
takes real wall-clock time on a table this size, this is not instant.

Revision ID: 0012
Revises: 0011
Create Date: 2026-07-30 19:30:00.000000

"""
from alembic import op

revision = '0012'
down_revision = '0011'
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE fno_bhavcopy "
        "ADD INDEX ix_bhav_symbol_instrument_date (symbol, instrument, trade_date), "
        "ALGORITHM=INPLACE, LOCK=NONE"
    )
    op.execute(
        "ALTER TABLE fno_bhavcopy "
        "ADD INDEX ix_bhav_symbol_instrument_expiry (symbol, instrument, expiry_dt), "
        "ALGORITHM=INPLACE, LOCK=NONE"
    )
    op.execute(
        "ALTER TABLE fno_bhavcopy "
        "ADD INDEX ix_bhav_symbol_instrument_expiry_date (symbol, instrument, expiry_dt, trade_date), "
        "ALGORITHM=INPLACE, LOCK=NONE"
    )


def downgrade():
    op.execute("ALTER TABLE fno_bhavcopy DROP INDEX ix_bhav_symbol_instrument_expiry_date, ALGORITHM=INPLACE, LOCK=NONE")
    op.execute("ALTER TABLE fno_bhavcopy DROP INDEX ix_bhav_symbol_instrument_expiry, ALGORITHM=INPLACE, LOCK=NONE")
    op.execute("ALTER TABLE fno_bhavcopy DROP INDEX ix_bhav_symbol_instrument_date, ALGORITHM=INPLACE, LOCK=NONE")
