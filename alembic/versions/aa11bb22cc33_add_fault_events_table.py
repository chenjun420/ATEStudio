"""add_fault_events_table

RH-1 (v41-remaining-hardening #1)：故障事件持久化表，设计文档 §8.3
历史故障热力图数据源。写入方：T44 fault-injection / T38 manual-fault
（NATS 发布成功后落库，失败仅告警）；读取方：
GET /fixtures/{fixture_id}/fault-stats 聚合 count/last_seen。

Revision ID: aa11bb22cc33
Revises: a5f6b7c8d9e0
Create Date: 2026-08-26

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'aa11bb22cc33'
down_revision: Union[str, Sequence[str], None] = 'a5f6b7c8d9e0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('fault_events',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('fixture_id', sa.String(36), nullable=True),
        sa.Column('run_id', sa.String(64), nullable=True),
        sa.Column('link_id', sa.String(200), nullable=False),
        sa.Column('fault_type', sa.String(100), nullable=False),
        sa.Column('source', sa.String(20), nullable=False),
        sa.Column('detail', sa.JSON().with_variant(postgresql.JSONB(), 'postgresql'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.ForeignKeyConstraint(['fixture_id'], ['fixture_topologies.id']),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_fault_events_run_id'), 'fault_events', ['run_id'], unique=False)
    op.create_index(op.f('ix_fault_events_link_id'), 'fault_events', ['link_id'], unique=False)
    op.create_index('ix_fault_events_fixture_id_created_at', 'fault_events', ['fixture_id', 'created_at'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_fault_events_fixture_id_created_at', table_name='fault_events')
    op.drop_index(op.f('ix_fault_events_link_id'), table_name='fault_events')
    op.drop_index(op.f('ix_fault_events_run_id'), table_name='fault_events')
    op.drop_table('fault_events')
