"""Add platform column to ctfs (ctfd | rctf)

Revision ID: 0005_platform_column
Revises: 0004_admin_role
Create Date: 2026-04-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_platform_column"
down_revision = "0004_admin_role"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ctfs",
        sa.Column(
            "platform",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'ctfd'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("ctfs", "platform")
