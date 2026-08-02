"""add_calibration_records_table

Revision ID: d7e8f9a0b1c2
Revises: c4d5e6f7a8b9
Create Date: 2026-08-02 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd7e8f9a0b1c2'
down_revision: Union[str, Sequence[str], None] = 'c4d5e6f7a8b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    Create the ``calibration_records`` table for instrument calibration
    tracking. The ``status`` column stores VALID/EXPIRING/EXPIRED as a
    plain string (no DB-level enum) so SQLite and PostgreSQL both work
    without enum migration headaches.
    """
    op.create_table('calibration_records',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('instrument_id', sa.String(255), nullable=False),
        sa.Column('last_calibration', sa.DateTime(timezone=True), nullable=False),
        sa.Column('interval_days', sa.Integer(), nullable=False),
        sa.Column('next_due', sa.DateTime(timezone=True), nullable=False),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column(
            'created_at', sa.DateTime(timezone=True),
            server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False,
        ),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True),
            server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False,
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_calibration_records_instrument_id'),
        'calibration_records', ['instrument_id'], unique=False,
    )
    op.create_index(
        op.f('ix_calibration_records_next_due'),
        'calibration_records', ['next_due'], unique=False,
    )
    op.create_index(
        op.f('ix_calibration_records_status'),
        'calibration_records', ['status'], unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_calibration_records_status'), table_name='calibration_records')
    op.drop_index(op.f('ix_calibration_records_next_due'), table_name='calibration_records')
    op.drop_index(op.f('ix_calibration_records_instrument_id'), table_name='calibration_records')
    op.drop_table('calibration_records')
