"""Add github_copilot_oauth_token_enc to user_settings

Revision ID: 0008_copilot_token
Revises: 0007_task_solves
Create Date: 2026-04-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_copilot_token"
down_revision = "0007_task_solves"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_settings",
        sa.Column(
            "github_copilot_oauth_token_enc",
            sa.LargeBinary(),
            nullable=False,
            server_default=sa.text("''::bytea"),
        ),
    )


def downgrade() -> None:
    op.drop_column("user_settings", "github_copilot_oauth_token_enc")
