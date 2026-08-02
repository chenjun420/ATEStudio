"""add_test_limits_table

Revision ID: f7a8b9c1d2e3
Revises: d6234a66c136
Create Date: 2026-08-01 13:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f7a8b9c1d2e3'
down_revision: Union[str, Sequence[str], None] = 'd6234a66c136'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('test_limits',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('limit_id', sa.String(255), nullable=False),
        sa.Column('product_type', sa.String(255), nullable=False),
        sa.Column('test_name', sa.String(255), nullable=False),
        sa.Column('spec_low', sa.Float(), nullable=False),
        sa.Column('spec_high', sa.Float(), nullable=False),
        sa.Column('unit', sa.String(64), nullable=False),
        sa.Column('effective_from', sa.Date(), nullable=False),
        sa.Column('effective_until', sa.Date(), nullable=True),
        sa.Column(
            'created_at', sa.DateTime(timezone=True),
            server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False,
        ),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True),
            server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False,
        ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_test_limits_limit_id'), 'test_limits', ['limit_id'], unique=False)
    op.create_index(op.f('ix_test_limits_product_type'), 'test_limits', ['product_type'], unique=False)
    op.create_index(op.f('ix_test_limits_test_name'), 'test_limits', ['test_name'], unique=False)
    op.create_index(op.f('ix_test_limits_effective_from'), 'test_limits', ['effective_from'], unique=False)
    op.create_index(op.f('ix_test_limits_effective_until'), 'test_limits', ['effective_until'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_test_limits_effective_until'), table_name='test_limits')
    op.drop_index(op.f('ix_test_limits_effective_from'), table_name='test_limits')
    op.drop_index(op.f('ix_test_limits_test_name'), table_name='test_limits')
    op.drop_index(op.f('ix_test_limits_product_type'), table_name='test_limits')
    op.drop_index(op.f('ix_test_limits_limit_id'), table_name='test_limits')
    op.drop_table('test_limits')
