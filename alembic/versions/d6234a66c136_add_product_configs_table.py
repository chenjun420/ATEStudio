"""add_product_configs_table

Revision ID: d6234a66c136
Revises: b8c4d5e6f7a8
Create Date: 2026-08-01 12:54:04.966876

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import sqlite


# revision identifiers, used by Alembic.
revision: str = 'd6234a66c136'
down_revision: Union[str, Sequence[str], None] = 'b8c4d5e6f7a8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('product_configs',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('product_type', sa.String(255), nullable=False),
        sa.Column('test_sequence_ref', sa.String(255), nullable=False),
        sa.Column('test_limits', sa.JSON(), nullable=False),
        sa.Column('instrument_assignments', sa.JSON(), nullable=False),
        sa.Column('checkpoints', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_product_configs_product_type'), 'product_configs', ['product_type'], unique=True)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_product_configs_product_type'), table_name='product_configs')
    op.drop_table('product_configs')
