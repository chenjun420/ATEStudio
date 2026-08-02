"""add_trace_fields_to_executions

Revision ID: e8f9a0b1c2d3
Revises: d7e8f9a0b1c2
Create Date: 2026-08-02 11:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e8f9a0b1c2d3'
down_revision: Union[str, Sequence[str], None] = 'd7e8f9a0b1c2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    Add DUT traceability fields to the ``executions`` table:
    - ``dut_serial`` (indexed) - the device-under-test serial number.
    - ``station_id`` - the station that ran the execution.
    - ``instrument_ids`` (JSON list) - instruments participating in the run.

    All three are nullable: existing executions (and the executions table
    created by a9f3c7e2d1b4) carry no traceability data until backfilled.
    """
    op.add_column(
        'executions',
        sa.Column('dut_serial', sa.String(255), nullable=True),
    )
    op.add_column(
        'executions',
        sa.Column('station_id', sa.String(255), nullable=True),
    )
    op.add_column(
        'executions',
        sa.Column('instrument_ids', sa.JSON(), nullable=True),
    )
    op.create_index(
        op.f('ix_executions_dut_serial'),
        'executions',
        ['dut_serial'],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_executions_dut_serial'), table_name='executions')
    op.drop_column('executions', 'instrument_ids')
    op.drop_column('executions', 'station_id')
    op.drop_column('executions', 'dut_serial')
