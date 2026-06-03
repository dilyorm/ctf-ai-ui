"""Add tasks + task_attachments for the /team kanban

Revision ID: 0006_tasks
Revises: 0005_platform_column
Create Date: 2026-04-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_tasks"
down_revision = "0005_platform_column"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "ctf_id",
            sa.Integer(),
            sa.ForeignKey("ctfs.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("external_id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("category", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("points", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("platform_description_md", sa.Text(), nullable=False, server_default=""),
        sa.Column("files_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("connection_info", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("description_override_md", sa.Text(), nullable=False, server_default=""),
        sa.Column("notes_md", sa.Text(), nullable=False, server_default=""),
        sa.Column("writeup_md", sa.Text(), nullable=False, server_default=""),
        sa.Column("flag", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="todo", index=True),
        sa.Column("assignee_type", sa.String(length=8), nullable=True),
        sa.Column(
            "assignee_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_solver_status", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("solved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("ctf_id", "external_id", name="uq_task_ctf_external"),
    )

    op.create_table(
        "task_attachments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "task_id",
            sa.Integer(),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="file"),
        sa.Column("filename", sa.String(length=300), nullable=False),
        sa.Column(
            "content_type",
            sa.String(length=120),
            nullable=False,
            server_default="application/octet-stream",
        ),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column(
            "uploaded_by_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("task_attachments")
    op.drop_table("tasks")
