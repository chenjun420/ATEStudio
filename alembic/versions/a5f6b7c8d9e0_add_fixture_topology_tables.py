"""add_fixture_topology_tables

设计文档 §9.4.1 工装拓扑表：fixture_topologies / fixture_versions / fixture_device_templates。

Revision ID: a5f6b7c8d9e0
Revises: e6f7a8b9c0d1
Create Date: 2026-08-17

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a5f6b7c8d9e0'
down_revision: Union[str, Sequence[str], None] = 'e6f7a8b9c0d1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('fixture_topologies',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('name', sa.String(200), nullable=False),
        sa.Column('version', sa.String(50), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('product_model', sa.String(100), nullable=True),
        sa.Column('topology_data', sa.JSON(), nullable=False),
        sa.Column('created_by', sa.String(100), nullable=True),
        sa.Column('tags', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name', 'version', name='uq_fixture_topologies_name_version')
    )

    op.create_table('fixture_versions',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('topology_id', sa.String(36), nullable=False),
        sa.Column('version', sa.String(50), nullable=False),
        sa.Column('change_log', sa.Text(), nullable=True),
        sa.Column('topology_data', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.ForeignKeyConstraint(['topology_id'], ['fixture_topologies.id']),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_fixture_versions_topology_id'), 'fixture_versions', ['topology_id'], unique=False)

    op.create_table('fixture_device_templates',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('category', sa.String(50), nullable=False),
        sa.Column('type', sa.String(50), nullable=False),
        sa.Column('model', sa.String(100), nullable=False),
        sa.Column('manufacturer', sa.String(100), nullable=True),
        sa.Column('spec_data', sa.JSON(), nullable=False),
        sa.Column('icon', sa.String(50), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_fixture_versions_topology_id'), table_name='fixture_versions')
    op.drop_table('fixture_versions')
    op.drop_table('fixture_topologies')
    op.drop_table('fixture_device_templates')
