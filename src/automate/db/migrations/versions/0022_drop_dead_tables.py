"""drop order_executions and candles (dead, unreferenced tables)

order_executions: routes_orders.py exposed a full CRUD-style order-
tracking API around this table, but nothing in the frontend ever called
it (confirmed via grep of frontend/src) and nothing in the backend ever
wrote to it outside that same dead router — a real broker-order-status
sync was never wired up. candles: write-only sink for
scripts/import_historical_csvs_to_db.py; nothing in the codebase ever
reads from it — backtest/data_feed.py reads the source CSVs directly
from data/historical/, not this table.

Revision ID: 0022
Revises: 0021
Create Date: 2026-08-02 00:00:00.000000

"""
from alembic import op


revision = '0022'
down_revision = '0021'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_table('order_executions')
    op.drop_table('candles')


def downgrade():
    raise NotImplementedError(
        "order_executions/candles were dropped as dead schema with no live "
        "reader/writer — recreate manually from an earlier migration if truly needed."
    )
