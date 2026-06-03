"""Add solves column to tasks

Revision ID: 0007_task_solves
Revises: 0006_tasks
Create Date: 2026-04-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_task_solves"
down_revision = "0006_tasks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("solves", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("tasks", "solves")
