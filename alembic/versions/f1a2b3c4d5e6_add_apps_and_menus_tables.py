"""add_apps_and_menus_tables

Revision ID: f1a2b3c4d5e6
Revises: e8f9a0b1c2d3
Create Date: 2026-08-02 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f1a2b3c4d5e6'
down_revision: Union[str, Sequence[str], None] = 'e8f9a0b1c2d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('apps',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('code', sa.String(64), nullable=False),
        sa.Column('name', sa.String(128), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('icon', sa.String(64), nullable=True),
        sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code'),
    )
    op.create_index('ix_apps_code', 'apps', ['code'])

    op.create_table('app_menus',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('app_id', sa.String(36), nullable=False),
        sa.Column('parent_id', sa.String(36), nullable=True),
        sa.Column('code', sa.String(64), nullable=False),
        sa.Column('name', sa.String(128), nullable=False),
        sa.Column('route_path', sa.String(256), nullable=False),
        sa.Column('route_name', sa.String(128), nullable=True),
        sa.Column('icon', sa.String(64), nullable=True),
        sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['app_id'], ['apps.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['parent_id'], ['app_menus.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_app_menus_app_id', 'app_menus', ['app_id'])
    op.create_index('ix_app_menus_parent_id', 'app_menus', ['parent_id'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_app_menus_parent_id', table_name='app_menus')
    op.drop_index('ix_app_menus_app_id', table_name='app_menus')
    op.drop_table('app_menus')
    op.drop_index('ix_apps_code', table_name='apps')
    op.drop_table('apps')
