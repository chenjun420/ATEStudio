"""add_rbac_tables

Revision ID: d5e6f7a8b9c0
Revises: b3c4d5e6f7a9
Create Date: 2026-08-02 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd5e6f7a8b9c0'
down_revision: Union[str, Sequence[str], None] = 'b3c4d5e6f7a9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema. Create roles and permissions tables."""
    op.create_table('roles',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('name', sa.String(50), nullable=False),
        sa.Column('description', sa.String(500), nullable=True),
        sa.Column('is_system', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('permissions', sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name'),
    )
    op.create_index('ix_roles_name', 'roles', ['name'], unique=True)

    op.create_table('permissions',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('code', sa.String(100), nullable=False),
        sa.Column('module', sa.String(50), nullable=False),
        sa.Column('description', sa.String(500), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code'),
    )
    op.create_index('ix_permissions_code', 'permissions', ['code'], unique=True)


def downgrade() -> None:
    """Downgrade schema. Drop permissions and roles tables."""
    op.drop_index('ix_permissions_code', table_name='permissions')
    op.drop_table('permissions')
    op.drop_index('ix_roles_name', table_name='roles')
    op.drop_table('roles')
