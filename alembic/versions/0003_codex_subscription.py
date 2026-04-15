"""Add codex CLI subscription columns to user_settings

Revision ID: 0003_codex_subscription
Revises: 0002_user_settings
Create Date: 2026-04-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_codex_subscription"
down_revision = "0002_user_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_settings",
        sa.Column(
            "codex_cli_path",
            sa.String(length=500),
            nullable=False,
            server_default=sa.text("''"),
        ),
    )
    op.add_column(
        "user_settings",
        sa.Column(
            "codex_config_dir",
            sa.String(length=500),
            nullable=False,
            server_default=sa.text("''"),
        ),
    )


def downgrade() -> None:
    op.drop_column("user_settings", "codex_config_dir")
    op.drop_column("user_settings", "codex_cli_path")
