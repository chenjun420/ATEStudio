"""add_node_flow_bindings_table

Revision ID: a2b3c4d5e6f7
Revises: f1a2b3c4d5e6
Create Date: 2026-08-02 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a2b3c4d5e6f7'
down_revision: Union[str, Sequence[str], None] = 'f1a2b3c4d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('node_flow_bindings',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('worker_id', sa.String(255), nullable=False),
        sa.Column('sequence_id', sa.String(36), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('priority', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('config', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['sequence_id'], ['sequences.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_node_flow_bindings_worker_id', 'node_flow_bindings', ['worker_id'])
    op.create_index('ix_node_flow_bindings_sequence_id', 'node_flow_bindings', ['sequence_id'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_node_flow_bindings_sequence_id', table_name='node_flow_bindings')
    op.drop_index('ix_node_flow_bindings_worker_id', table_name='node_flow_bindings')
    op.drop_table('node_flow_bindings')
