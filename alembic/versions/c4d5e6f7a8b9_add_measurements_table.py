"""add_measurements_table

Revision ID: c4d5e6f7a8b9
Revises: b3c4d5e6f7a8
Create Date: 2026-08-01 22:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4d5e6f7a8b9'
down_revision: Union[str, Sequence[str], None] = 'b3c4d5e6f7a8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    Create the ``measurements`` table for SPC analysis and historical
    traceability. Each row is one structured measurement (name, value,
    limits, outcome) tied to a DUT and execution.
    """
    op.create_table('measurements',
        sa.Column('measurement_id', sa.String(36), nullable=False),
        sa.Column('execution_ref', sa.String(36), nullable=True),
        sa.Column('station_ref', sa.String(255), nullable=True),
        sa.Column('product_ref', sa.String(255), nullable=False),
        sa.Column('dut_serial', sa.String(255), nullable=False),
        sa.Column('timestamp', sa.DateTime(timezone=True), nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('value', sa.Float(), nullable=True),
        sa.Column('limits_min', sa.Float(), nullable=True),
        sa.Column('limits_max', sa.Float(), nullable=True),
        sa.Column('unit', sa.String(64), nullable=True),
        sa.Column('outcome', sa.String(20), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.PrimaryKeyConstraint('measurement_id'),
    )
    op.create_index(op.f('ix_measurements_measurement_id'), 'measurements', ['measurement_id'], unique=False)
    op.create_index(op.f('ix_measurements_execution_ref'), 'measurements', ['execution_ref'], unique=False)
    op.create_index(op.f('ix_measurements_station_ref'), 'measurements', ['station_ref'], unique=False)
    op.create_index(op.f('ix_measurements_product_ref'), 'measurements', ['product_ref'], unique=False)
    op.create_index(op.f('ix_measurements_timestamp'), 'measurements', ['timestamp'], unique=False)
    op.create_index('ix_measurements_product_name_ts', 'measurements',
                    ['product_ref', 'name', 'timestamp'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_measurements_product_name_ts', table_name='measurements')
    op.drop_index(op.f('ix_measurements_timestamp'), table_name='measurements')
    op.drop_index(op.f('ix_measurements_product_ref'), table_name='measurements')
    op.drop_index(op.f('ix_measurements_station_ref'), table_name='measurements')
    op.drop_index(op.f('ix_measurements_execution_ref'), table_name='measurements')
    op.drop_index(op.f('ix_measurements_measurement_id'), table_name='measurements')
    op.drop_table('measurements')
