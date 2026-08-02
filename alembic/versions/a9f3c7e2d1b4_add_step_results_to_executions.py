"""add step_results to executions

Revision ID: a9f3c7e2d1b4
Revises: ec6f5c458757
Create Date: 2026-08-01 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a9f3c7e2d1b4'
down_revision: Union[str, Sequence[str], None] = 'ec6f5c458757'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    Create the ``executions`` table with all columns from the ORM model
    (including ``step_results``). No prior migration created this table, so a
    bare ``add_column`` would fail on a fresh database.
    """
    op.create_table('executions',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('sequence_id', sa.String(36), nullable=True),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('config', sa.JSON(), nullable=True),
        sa.Column('result', sa.JSON(), nullable=True),
        sa.Column('step_results', sa.JSON(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True),
                  server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_executions_sequence_id'), 'executions', ['sequence_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_executions_sequence_id'), table_name='executions')
    op.drop_table('executions')
