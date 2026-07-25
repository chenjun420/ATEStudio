"""add node_templates table

Revision ID: ec6f5c458757
Revises: fb11e0780a2a
Create Date: 2026-07-19 22:28:14.735352

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import sqlite

# revision identifiers, used by Alembic.
revision: str = 'ec6f5c458757'
down_revision: Union[str, Sequence[str], None] = 'fb11e0780a2a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Create node_templates table
    op.create_table('node_templates',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('type', sa.String(50), nullable=False),
        sa.Column('appearance', sa.JSON(), nullable=True),
        sa.Column('default_data', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_node_templates_name'), 'node_templates', ['name'], unique=True)
    op.create_index(op.f('ix_node_templates_type'), 'node_templates', ['type'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_node_templates_type'), table_name='node_templates')
    op.drop_index(op.f('ix_node_templates_name'), table_name='node_templates')
    op.drop_table('node_templates')