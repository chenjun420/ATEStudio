"""add_users_table

Revision ID: b3c4d5e6f7a9
Revises: a2b3c4d5e6f7
Create Date: 2026-08-02 13:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b3c4d5e6f7a9'
down_revision: Union[str, Sequence[str], None] = 'a2b3c4d5e6f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema — add theme_mode, language, updated_at to users table.

    The users table may already exist (created by b8c4d5e6f7a8).
    We only add the missing columns for preferences + user management.
    """
    import sqlalchemy as sa
    from sqlalchemy import inspect

    bind = op.get_bind()
    inspector = inspect(bind)

    if not inspector.has_table('users'):
        # Users table doesn't exist — create it with all columns
        op.create_table('users',
            sa.Column('id', sa.String(36), nullable=False),
            sa.Column('username', sa.String(255), nullable=False),
            sa.Column('password_hash', sa.String(255), nullable=False),
            sa.Column('role', sa.String(50), nullable=False),
            sa.Column('scopes', sa.JSON(), nullable=True),
            sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
            sa.Column('theme_mode', sa.String(20), nullable=False, server_default='auto'),
            sa.Column('language', sa.String(10), nullable=False, server_default='en'),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('username'),
        )
        op.create_index('ix_users_username', 'users', ['username'])
    else:
        # Users table exists — add missing columns
        existing_columns = {col['name'] for col in inspector.get_columns('users')}
        if 'theme_mode' not in existing_columns:
            op.add_column('users', sa.Column('theme_mode', sa.String(20), nullable=False, server_default='auto'))
        if 'language' not in existing_columns:
            op.add_column('users', sa.Column('language', sa.String(10), nullable=False, server_default='en'))
        if 'updated_at' not in existing_columns:
            op.add_column('users', sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))


def downgrade() -> None:
    """Downgrade schema — remove theme_mode, language, updated_at columns."""
    import sqlalchemy as sa
    from sqlalchemy import inspect

    bind = op.get_bind()
    inspector = inspect(bind)

    if inspector.has_table('users'):
        existing_columns = {col['name'] for col in inspector.get_columns('users')}
        if 'updated_at' in existing_columns:
            op.drop_column('users', 'updated_at')
        if 'language' in existing_columns:
            op.drop_column('users', 'language')
        if 'theme_mode' in existing_columns:
            op.drop_column('users', 'theme_mode')
