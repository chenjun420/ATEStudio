"""add_breakpoints_table

Revision ID: b3c4d5e6f7a8
Revises: a1b2c3d4e5f6
Create Date: 2026-08-01 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b3c4d5e6f7a8'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('breakpoints',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('session_id', sa.String(36), nullable=False),
        sa.Column('step_id', sa.String(255), nullable=False),
        sa.Column('node_id', sa.String(255), nullable=False),
        sa.Column('line_number', sa.Integer(), nullable=False),
        sa.Column('condition', sa.String(1024), nullable=True),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('node_data', sa.JSON(), nullable=True),
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
    op.create_index(op.f('ix_breakpoints_session_id'), 'breakpoints', ['session_id'], unique=False)
    op.create_index(op.f('ix_breakpoints_step_id'), 'breakpoints', ['step_id'], unique=False)
    op.create_index(op.f('ix_breakpoints_node_id'), 'breakpoints', ['node_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_breakpoints_node_id'), table_name='breakpoints')
    op.drop_index(op.f('ix_breakpoints_step_id'), table_name='breakpoints')
    op.drop_index(op.f('ix_breakpoints_session_id'), table_name='breakpoints')
    op.drop_table('breakpoints')
