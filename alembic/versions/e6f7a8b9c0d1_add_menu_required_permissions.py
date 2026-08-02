"""add_menu_required_permissions

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-08-02 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e6f7a8b9c0d1'
down_revision: Union[str, Sequence[str], None] = 'd5e6f7a8b9c0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema. Add required_permissions column to app_menus table."""
    op.add_column('app_menus', sa.Column('required_permissions', sa.JSON(), nullable=True))


def downgrade() -> None:
    """Downgrade schema. Remove required_permissions column from app_menus table."""
    op.drop_column('app_menus', 'required_permissions')
