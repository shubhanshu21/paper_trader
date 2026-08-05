"""
Add user watchlists and order execution tracking tables.

These tables enable:
- User-specific watchlist persistence
- Real-time order status tracking from broker
- Better portfolio management and monitoring
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade():
    # Create user_watchlists table
    op.create_table(
        'user_watchlists',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.BigInteger(), nullable=False),
        sa.Column('instrument_key', sa.String(64), nullable=False),
        sa.Column('added_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('order_index', sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        mysql_charset='utf8mb4'
    )
    
    # Create indexes for user_watchlists
    op.create_index('ix_user_watchlists_user_id', 'user_watchlists', ['user_id'])
    op.create_index('ix_user_watchlists_instrument', 'user_watchlists', ['instrument_key'])
    op.create_index('ix_user_watchlists_user_instrument', 'user_watchlists', ['user_id', 'instrument_key'], unique=True)
    
    # Create order_executions table
    op.create_table(
        'order_executions',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.BigInteger(), nullable=True),
        sa.Column('order_id', sa.String(64), nullable=False),
        sa.Column('instrument_key', sa.String(64), nullable=False),
        sa.Column('direction', sa.String(8), nullable=False),
        sa.Column('quantity', sa.Integer(), nullable=False),
        sa.Column('price', sa.Numeric(12, 4), nullable=True),
        sa.Column('product', sa.String(8), nullable=False),
        sa.Column('mode', sa.String(8), nullable=False),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('status_message', sa.String(256), nullable=True),
        sa.Column('filled_quantity', sa.Integer(), nullable=True),
        sa.Column('filled_price', sa.Numeric(12, 4), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('strategy_name', sa.String(128), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('order_id'),
        mysql_charset='utf8mb4'
    )
    
    # Create indexes for order_executions
    op.create_index('ix_order_executions_user_id', 'order_executions', ['user_id'])
    op.create_index('ix_order_executions_order_id', 'order_executions', ['order_id'])
    op.create_index('ix_order_executions_status', 'order_executions', ['status'])
    op.create_index('ix_order_executions_instrument', 'order_executions', ['instrument_key'])
    op.create_index('ix_order_executions_created_at', 'order_executions', ['created_at'])


def downgrade():
    # Drop order_executions table
    op.drop_index('ix_order_executions_created_at', table_name='order_executions')
    op.drop_index('ix_order_executions_instrument', table_name='order_executions')
    op.drop_index('ix_order_executions_status', table_name='order_executions')
    op.drop_index('ix_order_executions_order_id', table_name='order_executions')
    op.drop_index('ix_order_executions_user_id', table_name='order_executions')
    op.drop_table('order_executions')
    
    # Drop user_watchlists table
    op.drop_index('ix_user_watchlists_user_instrument', table_name='user_watchlists')
    op.drop_index('ix_user_watchlists_instrument', table_name='user_watchlists')
    op.drop_index('ix_user_watchlists_user_id', table_name='user_watchlists')
    op.drop_table('user_watchlists')
