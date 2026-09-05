"""add_knowledge_tables

Task 10 (breakpoint-fmea-architecture, Wave 3): relational persistence for
the ontology-driven domain — deterministic schema, no LLM:

- ``test_requirements`` — verifiable requirements (product ref, code/title/
  description, provenance source dsl/atml/manual, optional ATML ref).
- ``test_cases`` — cases implementing requirements (nullable FK to allow
  ingestion ordering), DSL sequence/step refs and ATML ref, status.
- ``fmeas`` — FMEA entries with severity/occurrence/detection integers in
  [1, 10] (DB CHECK constraints; DISTINCT from the 3-level fixture Severity)
  and a stored ``rpn`` derived server-side as S*O*D by the model layer.
- ``diagnoses`` — persisted AI diagnoses linked to an execution run, with
  nullable operator feedback (helpful bool + note) for task 15.

Revision ID: d1e2f3a4b5c6
Revises: c9d0e1f2a3b4
Create Date: 2026-09-04 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd1e2f3a4b5c6'
down_revision: Union[str, Sequence[str], None] = 'c9d0e1f2a3b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema — create the four knowledge-domain tables."""
    op.create_table(
        'test_requirements',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('product_code', sa.String(length=100), nullable=False),
        sa.Column('requirement_code', sa.String(length=100), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('source', sa.String(length=20), nullable=False),
        sa.Column('atml_ref', sa.String(length=255), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True),
                  server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_test_requirements_product_code'),
                    'test_requirements', ['product_code'], unique=False)
    op.create_index(op.f('ix_test_requirements_requirement_code'),
                    'test_requirements', ['requirement_code'], unique=False)

    op.create_table(
        'test_cases',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('requirement_id', sa.String(length=36), nullable=True),
        sa.Column('case_code', sa.String(length=100), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('sequence_id', sa.String(length=36), nullable=True),
        sa.Column('step_id', sa.String(length=255), nullable=False),
        sa.Column('atml_ref', sa.String(length=255), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True),
                  server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.ForeignKeyConstraint(['requirement_id'], ['test_requirements.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_test_cases_requirement_id'),
                    'test_cases', ['requirement_id'], unique=False)
    op.create_index(op.f('ix_test_cases_case_code'),
                    'test_cases', ['case_code'], unique=False)
    op.create_index(op.f('ix_test_cases_sequence_id'),
                    'test_cases', ['sequence_id'], unique=False)
    op.create_index(op.f('ix_test_cases_step_id'),
                    'test_cases', ['step_id'], unique=False)

    op.create_table(
        'fmeas',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('component_code', sa.String(length=200), nullable=False),
        sa.Column('function_name', sa.String(length=255), nullable=True),
        sa.Column('fault_code', sa.String(length=100), nullable=True),
        sa.Column('failure_mode', sa.String(length=500), nullable=False),
        sa.Column('effects', sa.Text(), nullable=True),
        sa.Column('cause', sa.Text(), nullable=True),
        sa.Column('severity', sa.Integer(), nullable=False),
        sa.Column('occurrence', sa.Integer(), nullable=False),
        sa.Column('detection', sa.Integer(), nullable=False),
        sa.Column('rpn', sa.Integer(), nullable=False),
        sa.Column('recommended_action', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True),
                  server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.CheckConstraint('severity BETWEEN 1 AND 10',
                           name='ck_fmeas_severity_range'),
        sa.CheckConstraint('occurrence BETWEEN 1 AND 10',
                           name='ck_fmeas_occurrence_range'),
        sa.CheckConstraint('detection BETWEEN 1 AND 10',
                           name='ck_fmeas_detection_range'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_fmeas_component_code'),
                    'fmeas', ['component_code'], unique=False)
    op.create_index(op.f('ix_fmeas_fault_code'),
                    'fmeas', ['fault_code'], unique=False)

    op.create_table(
        'diagnoses',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('run_id', sa.String(length=36), nullable=True),
        sa.Column('session_id', sa.String(length=64), nullable=True),
        sa.Column('symptom', sa.Text(), nullable=False),
        sa.Column('conclusion', sa.Text(), nullable=True),
        sa.Column('context_summary', sa.Text(), nullable=True),
        sa.Column('helpful', sa.Boolean(), nullable=True),
        sa.Column('feedback_note', sa.Text(), nullable=True),
        sa.Column('llm_model', sa.String(length=100), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True),
                  server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.ForeignKeyConstraint(['run_id'], ['executions.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_diagnoses_run_id'),
                    'diagnoses', ['run_id'], unique=False)
    op.create_index(op.f('ix_diagnoses_session_id'),
                    'diagnoses', ['session_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema — drop the knowledge tables in reverse order."""
    op.drop_index(op.f('ix_diagnoses_session_id'), table_name='diagnoses')
    op.drop_index(op.f('ix_diagnoses_run_id'), table_name='diagnoses')
    op.drop_table('diagnoses')

    op.drop_index(op.f('ix_fmeas_fault_code'), table_name='fmeas')
    op.drop_index(op.f('ix_fmeas_component_code'), table_name='fmeas')
    op.drop_table('fmeas')

    op.drop_index(op.f('ix_test_cases_step_id'), table_name='test_cases')
    op.drop_index(op.f('ix_test_cases_sequence_id'), table_name='test_cases')
    op.drop_index(op.f('ix_test_cases_case_code'), table_name='test_cases')
    op.drop_index(op.f('ix_test_cases_requirement_id'), table_name='test_cases')
    op.drop_table('test_cases')

    op.drop_index(op.f('ix_test_requirements_requirement_code'),
                  table_name='test_requirements')
    op.drop_index(op.f('ix_test_requirements_product_code'),
                  table_name='test_requirements')
    op.drop_table('test_requirements')
