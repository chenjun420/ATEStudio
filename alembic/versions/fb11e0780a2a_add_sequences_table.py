"""add_sequences_table

Revision ID: fb11e0780a2a
Revises: eecd23d2d8e8
Create Date: 2026-07-19 20:35:57.392760

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import sqlite

# revision identifiers, used by Alembic.
revision: str = 'fb11e0780a2a'
down_revision: Union[str, Sequence[str], None] = 'eecd23d2d8e8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # sequences table already created in eecd23d2d8e8 — this migration is a no-op
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass