"""add_kind_target_to_breakpoints

Revision ID: c9d0e1f2a3b4
Revises: aa11bb22cc33
Create Date: 2026-08-31 10:00:00.000000

Adds the T39 (v41-gap-analysis #39, §8.4) typed-breakpoint columns
``kind`` and ``target`` to the existing ``breakpoints`` table (created by
b3c4d5e6f7a8). Both are NULLABLE so legacy debugpy rows (task 21 removal)
keep their shape; durable T39 rows always populate them. The existing
``session_id`` column stores the execution ``run_id`` for T39 rows.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c9d0e1f2a3b4'
down_revision: Union[str, Sequence[str], None] = 'aa11bb22cc33'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema — add nullable kind/target columns to breakpoints."""
    op.add_column(
        'breakpoints',
        sa.Column('kind', sa.String(length=32), nullable=True),
    )
    op.add_column(
        'breakpoints',
        sa.Column('target', sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema — drop the T39 typed columns."""
    op.drop_column('breakpoints', 'target')
    op.drop_column('breakpoints', 'kind')
