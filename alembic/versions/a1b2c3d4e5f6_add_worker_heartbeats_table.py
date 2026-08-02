"""add_worker_heartbeats_table

Revision ID: a1b2c3d4e5f6
Revises: f7a8b9c1d2e3
Create Date: 2026-08-01 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = 'f7a8b9c1d2e3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('worker_heartbeats',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('worker_id', sa.String(255), nullable=False),
        sa.Column('hostname', sa.String(255), nullable=False),
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('capabilities', sa.JSON(), nullable=False),
        sa.Column('current_tasks', sa.Integer(), nullable=False),
        sa.Column('recorded_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            'created_at', sa.DateTime(timezone=True),
            server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False,
        ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_worker_heartbeats_worker_id'), 'worker_heartbeats', ['worker_id'], unique=False)
    op.create_index(op.f('ix_worker_heartbeats_recorded_at'), 'worker_heartbeats', ['recorded_at'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_worker_heartbeats_recorded_at'), table_name='worker_heartbeats')
    op.drop_index(op.f('ix_worker_heartbeats_worker_id'), table_name='worker_heartbeats')
    op.drop_table('worker_heartbeats')
