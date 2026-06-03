"""Add role + display_name to users

Revision ID: 0004_admin_role
Revises: 0003_codex_subscription
Create Date: 2026-04-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_admin_role"
down_revision = "0003_codex_subscription"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "role",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'member'"),
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "display_name",
            sa.String(length=120),
            nullable=False,
            server_default=sa.text("''"),
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "display_name")
    op.drop_column("users", "role")
